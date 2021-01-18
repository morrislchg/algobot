import sqlite3
import time
import os

from datetime import timedelta, timezone, datetime
from helpers import get_logger, ROOT_DIR, get_ups_and_downs, get_data_from_parameter
from contextlib import closing
from binance.client import Client
from binance.helpers import interval_to_milliseconds


class Data:
    def __init__(self, interval: str = '1h', symbol: str = 'BTCUSDT', loadData: bool = True,
                 updateData: bool = True, log: bool = False, logFile: str = 'data', logObject=None):
        """
        Data object that will retrieve current and historical prices from the Binance API and calculate moving averages.
        :param interval: Interval for which the data object will track prices.
        :param symbol: Symbol for which the data object will track prices.
        :param: loadData: Boolean for whether data will be loaded or not.
        :param: updateData: Boolean for whether data will be updated if it is loaded.
        """
        self.binanceClient = Client()  # Initialize Binance client
        self.logger = self.get_logging_object(log=log, logFile=logFile, logObject=logObject)
        self.validate_interval(interval)
        self.interval = interval
        self.intervalUnit, self.intervalMeasurement = self.get_interval_unit_and_measurement()

        self.downloadCompleted = False
        self.downloadLoop = True

        symbol = symbol.upper()
        self.validate_symbol(symbol)
        self.symbol = symbol  # Symbol of data being used.
        self.data = []  # Total bot data.
        self.ema_data = {}  # Cached past EMA data for memoization.
        self.rsi_data = {}  # Cached past RSI data for memoization.

        self.databaseTable = f'data_{self.interval}'
        self.databaseFile = self.get_database_file()
        self.create_table()

        if loadData:
            # Create, initialize, store, and get values from database.
            self.load_data(update=updateData)

    @staticmethod
    def get_logging_object(log: bool, logFile: str, logObject):
        """
        Returns a logger object.
        :param log: Boolean that determines where logging is enabled or not.
        :param logFile: File to log to.
        :param logObject: Log object to return if there is one already specified.
        :return: Logger object or None.
        """
        if logObject is not None:
            return logObject
        else:
            if not log:
                return None
            else:
                return get_logger(logFile, logFile)

    def validate_interval(self, interval: str):
        """
        Validates interval. If incorrect interval, raises ValueError.
        :param interval: Interval to be checked.
        """
        if not self.is_valid_interval(interval):
            raise ValueError(f'Invalid interval {interval} specified.')

    def validate_symbol(self, symbol: str):
        """
        Validates symbol for data to be retrieved. Raises ValueError if symbol type is incorrect.
        :param symbol: Symbol to be checked.
        """
        if not self.is_valid_symbol(symbol):
            raise ValueError(f'Invalid symbol {symbol} specified.')

    def load_data(self, update: bool = True):
        """
        Loads data to Data object.
        :param update: Boolean that determines whether data is updated or not.
        """
        self.get_data_from_database()
        if update:
            if not self.database_is_updated():
                self.output_message("Updating data...")
                self.update_database_and_data()
            else:
                self.output_message("Database is up-to-date.")

    def output_message(self, message: str, level=2, printMessage: bool = False):
        """
        I need to research the logging module better, but in essence, this function just logs and optionally prints
        message provided.
        :param message: Messaged to be logged and potentially printed.
        :param level: Level message will be logged at.
        :param printMessage: Boolean that decides whether message will also be printed or not.
        """
        if printMessage:
            print(message)

        if self.logger:
            if level == 2:
                self.logger.info(message)
            elif level == 3:
                self.logger.debug(message)
            elif level == 4:
                self.logger.warning(message)
            elif level == 5:
                self.logger.critical(message)

    def get_database_file(self) -> str:
        """
        Retrieves database file path.
        :return: Database file path.
        """
        currentPath = os.getcwd()
        os.chdir(ROOT_DIR)
        if not os.path.exists('Databases'):
            os.mkdir('Databases')

        filePath = os.path.join(os.getcwd(), 'Databases', f'{self.symbol}.db')
        os.chdir(currentPath)
        return filePath

    def create_table(self):
        """
        Creates a new table with interval if it does not exist
        """
        with closing(sqlite3.connect(self.databaseFile)) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(f'''
                                CREATE TABLE IF NOT EXISTS {self.databaseTable}(
                                date_utc TEXT PRIMARY KEY,
                                open_price TEXT NOT NULL,
                                high_price TEXT NOT NULL,
                                low_price TEXT NOT NULL,
                                close_price TEXT NOT NULL,
                                volume TEXT NOT NULL,
                                quote_asset_volume TEXT NOT NULL,
                                number_of_trades TEXT NOT NULL,
                                taker_buy_base_asset TEXT NOT NULL,
                                taker_buy_quote_asset TEXT NOT NULL
                                );''')
                connection.commit()

    def dump_to_table(self, totalData=None) -> bool:
        """
        Dumps date and price information to database.
        :return: A boolean whether data entry was successful or not.
        """
        if totalData is None:
            totalData = self.data

        query = f'''INSERT INTO {self.databaseTable} (date_utc, open_price, high_price, low_price, close_price,
                            volume, quote_asset_volume, number_of_trades, taker_buy_base_asset, taker_buy_quote_asset) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'''
        with closing(sqlite3.connect(self.databaseFile)) as connection:
            with closing(connection.cursor()) as cursor:
                for data in totalData:
                    try:
                        cursor.execute(query,
                                       (data['date_utc'].strftime('%Y-%m-%d %H:%M:%S'),
                                        data['open'],
                                        data['high'],
                                        data['low'],
                                        data['close'],
                                        data['volume'],
                                        data['quote_asset_volume'],
                                        data['number_of_trades'],
                                        data['taker_buy_base_asset'],
                                        data['taker_buy_quote_asset'],
                                        ))
                    except sqlite3.IntegrityError:
                        pass  # This just means the data already exists in the database, so ignore.
                    except sqlite3.OperationalError:
                        connection.commit()
                        self.output_message("Insertion to database failed. Will retry next run.", 4)
                        return False
            connection.commit()
        self.output_message("Successfully stored all new data to database.")
        return True

    def get_latest_database_row(self):
        """
        Returns the latest row from database table.
        :return: Row data or None depending on if value exists.
        """
        with closing(sqlite3.connect(self.databaseFile)) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(f'SELECT date_utc FROM {self.databaseTable} ORDER BY date_utc DESC LIMIT 1')
                return cursor.fetchone()

    def get_data_from_database(self):
        """
        Loads data from database and appends it to run-time data.
        """
        with closing(sqlite3.connect(self.databaseFile)) as connection:
            with closing(connection.cursor()) as cursor:
                rows = cursor.execute(f'''
                        SELECT "date_utc", "open_price", "high_price", "low_price", "close_price", "volume", 
                        "quote_asset_volume", "number_of_trades", "taker_buy_base_asset", "taker_buy_quote_asset"
                        FROM {self.databaseTable} ORDER BY date_utc DESC
                        ''').fetchall()

        if len(rows) > 0:
            self.output_message("Retrieving data from database...")
        else:
            self.output_message("No data found in database.")
            return

        for row in rows:
            date_utc = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            self.data.append({'date_utc': date_utc,
                              'open': float(row[1]),
                              'high': float(row[2]),
                              'low': float(row[3]),
                              'close': float(row[4]),
                              'volume': float(row[5]),
                              'quote_asset_volume': float(row[6]),
                              'number_of_trades': float(row[7]),
                              'taker_buy_base_asset': float(row[8]),
                              'taker_buy_quote_asset': float(row[9]),
                              })

    def database_is_updated(self) -> bool:
        """
        Checks if data is updated or not with database by interval provided in accordance to UTC time.
        :return: A boolean whether data is updated or not.
        """
        result = self.get_latest_database_row()
        if result is None:
            return False
        latestDate = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return self.is_latest_date(latestDate)

    # noinspection PyProtectedMember
    def get_latest_timestamp(self) -> int:
        """
        Returns latest timestamp available based on database.
        :return: Latest timestamp.
        """
        result = self.get_latest_database_row()
        if result is None:
            return self.binanceClient._get_earliest_valid_timestamp(self.symbol, self.interval)
        else:
            latestDate = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            return int(latestDate.timestamp()) * 1000  # Converting timestamp to milliseconds

    # noinspection PyProtectedMember
    def update_database_and_data(self):
        """
        Updates database by retrieving information from Binance API
        """
        result = self.get_latest_database_row()
        if result is None:  # Then get the earliest timestamp possible
            timestamp = self.binanceClient._get_earliest_valid_timestamp(self.symbol, self.interval)
            self.output_message(f'Downloading all available historical data for {self.interval} intervals.')
        else:
            latestDate = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            timestamp = int(latestDate.timestamp()) * 1000  # Converting timestamp to milliseconds
            dateWithIntervalAdded = latestDate + timedelta(minutes=self.get_interval_minutes())
            self.output_message(f"Previous data up to UTC {dateWithIntervalAdded} found.")

        if not self.database_is_updated():
            newData = self.get_new_data(timestamp)
            self.output_message("Successfully downloaded all new data.")
            self.output_message("Inserting data to live program...")
            self.insert_data(newData)
            self.output_message("Storing updated data to database...")
            self.dump_to_table(self.data[-len(newData):])
        else:
            self.output_message("Database is up-to-date.")

    # noinspection PyProtectedMember
    def custom_get_new_data(self, limit: int = 500, progress_callback=None, locked=None, removeFirst=False,
                            caller=-1):
        """
        Returns new data from Binance API from timestamp specified, however this one is custom-made.
        :param caller: Caller that called this function. Only used for botThread.
        :param removeFirst: Boolean whether newest data is removed or not.
        :param locked: Signal to emit back to GUI when storing data. Cannot be canceled once here.
        :param progress_callback: Signal to emit back to GUI to show progress.
        :param limit: Limit per pull.
        :return: A list of dictionaries.
        """
        # This code below is taken from binance client and slightly refactored.
        self.downloadLoop = True
        output_data = []  # Initialize our list
        timeframe = interval_to_milliseconds(self.interval)
        start_ts = total_beginning_timestamp = self.get_latest_timestamp()
        end_progress = time.time() * 1000 - total_beginning_timestamp
        idx = 0

        while True and self.downloadLoop:
            tempData = self.binanceClient.get_klines(
                symbol=self.symbol,
                interval=self.interval,
                limit=limit,
                startTime=start_ts,
                endTime=None
            )

            if not len(tempData):
                break

            output_data += tempData
            start_ts = tempData[-1][0]
            if progress_callback:
                progress = (start_ts - total_beginning_timestamp) / end_progress * 94
                progress_callback.emit(int(progress), "Downloading data...", caller)

            idx += 1
            # check if we received less than the required limit and exit the loop
            if len(tempData) < limit:
                # exit the while loop
                break

            # increment next call by our timeframe
            start_ts += timeframe

            # sleep after every 5th call to be kind to the API
            if idx % 5 == 0:
                time.sleep(1)

        if not self.downloadLoop:
            progress_callback.emit(-1, "Download canceled.", caller)
            return []

        if locked:
            locked.emit()

        if removeFirst:
            output_data = output_data[:-1]

        progress_callback.emit(95, "Saving data...", caller)
        self.insert_data(output_data)
        progress_callback.emit(97, "This may take a while. Dumping data to database...", caller)

        if removeFirst:  # We don't want current data as it's not the latest data.
            self.dump_to_table(self.data[:len(output_data)])
        else:
            self.dump_to_table(self.data[1:len(output_data)])

        progress_callback.emit(100, "Downloaded all new data successfully.", caller)
        self.downloadLoop = False
        self.downloadCompleted = True
        return self.data

    def get_new_data(self, timestamp, limit: int = 1000):
        """
        Returns new data from Binance API from timestamp specified.
        :param timestamp: Initial timestamp.
        :param limit: Limit per pull.
        :return: A list of dictionaries.
        """
        newData = self.binanceClient.get_historical_klines(self.symbol, self.interval, timestamp + 1, limit=limit)
        self.downloadCompleted = True
        return newData[:-1]  # Up to -1st index, because we don't want current period data.

    def is_latest_date(self, latestDate) -> bool:
        """
        Checks whether the latest date available is the latest period available.
        :param latestDate: Datetime object.
        :return: True or false whether date is latest period or not.
        """
        minutes = self.get_interval_minutes()
        return latestDate + timedelta(minutes=minutes) >= datetime.now(timezone.utc) - timedelta(minutes=minutes)

    def data_is_updated(self) -> bool:
        """
        Checks whether data is fully updated or not.
        :return: A boolean whether data is updated or not with Binance values.
        """
        latestDate = self.data[0]['date_utc']
        return self.is_latest_date(latestDate)

    def insert_data(self, newData: list):
        """
        Inserts data from newData to run-time data.
        :param newData: List with new data values.
        """
        for data in newData:
            parsedDate = datetime.fromtimestamp(int(data[0]) / 1000, tz=timezone.utc)
            self.data.insert(0, {'date_utc': parsedDate,
                                 'open': float(data[1]),
                                 'high': float(data[2]),
                                 'low': float(data[3]),
                                 'close': float(data[4]),
                                 'volume': float(data[5]),
                                 'quote_asset_volume': float(data[6]),
                                 'number_of_trades': float(data[7]),
                                 'taker_buy_base_asset': float(data[8]),
                                 'taker_buy_quote_asset': float(data[9]),
                                 })

    def update_data(self):
        """
        Updates run-time data with Binance API values.
        """
        latestDate = self.data[0]['date_utc']
        timestamp = int(latestDate.timestamp()) * 1000
        dateWithIntervalAdded = latestDate + timedelta(minutes=self.get_interval_minutes())
        self.output_message(f"Previous data found up to UTC {dateWithIntervalAdded}.")
        if not self.data_is_updated():
            newData = self.get_new_data(timestamp)
            self.insert_data(newData)
            self.output_message("Data has been updated successfully.")
        else:
            self.output_message("Data is up-to-date.")

    def get_current_data(self) -> dict:
        """
        Retrieves current market dictionary with open, high, low, close prices.
        :return: A dictionary with current open, high, low, and close prices.
        """
        try:
            if not self.data_is_updated():
                self.update_data()

            currentInterval = self.data[0]['date_utc'] + timedelta(minutes=self.get_interval_minutes())
            currentTimestamp = int(currentInterval.timestamp() * 1000)

            nextInterval = currentInterval + timedelta(minutes=self.get_interval_minutes())
            nextTimestamp = int(nextInterval.timestamp() * 1000) - 1
            currentData = self.binanceClient.get_klines(symbol=self.symbol,
                                                        interval=self.interval,
                                                        startTime=currentTimestamp,
                                                        endTime=nextTimestamp,
                                                        )[0]
            currentDataDictionary = {'date_utc': currentInterval,
                                     'open': float(currentData[1]),
                                     'high': float(currentData[2]),
                                     'low': float(currentData[3]),
                                     'close': float(currentData[4]),
                                     'volume:': float(currentData[5]),
                                     'quote_asset_volume:': float(currentData[6]),
                                     'number_of_trades:': float(currentData[7]),
                                     'taker_buy_base_asset:': float(currentData[8]),
                                     'taker_buy_quote_asset:': float(currentData[9]), }
            return currentDataDictionary
        except Exception as e:
            self.output_message(f"Error: {e}. Retrying in 5 seconds...", 4)
            time.sleep(5)
            return self.get_current_data()

    def get_current_price(self) -> float:
        """
        Returns the current market ticker price.
        :return: Ticker market price
        """
        try:
            return float(self.binanceClient.get_symbol_ticker(symbol=self.symbol)['price'])
        except Exception as e:
            self.output_message(f'Error: {e}. Retrying in 15 seconds...', 4)
            time.sleep(15)
            return self.get_current_price()

    def get_interval_unit_and_measurement(self) -> tuple:
        """
        Returns interval unit and measurement.
        :return: A tuple with interval unit and measurement respectively.
        """
        unit = self.interval[-1]  # Gets the unit of the interval. eg 12h = h
        measurement = int(self.interval[:-1])  # Gets the measurement, eg 12h = 12
        return unit, measurement

    def get_interval_minutes(self) -> int:
        """
        Returns interval minutes.
        :return: An integer representing the minutes for an interval.
        """
        if self.intervalUnit == 'h':
            return self.intervalMeasurement * 60
        elif self.intervalUnit == 'm':
            return self.intervalMeasurement
        elif self.intervalUnit == 'd':
            return self.intervalMeasurement * 24 * 60
        else:
            raise ValueError("Invalid interval.", 4)

    def write_csv_data(self, totalData: list, fileName: str, armyTime: bool = True) -> str:
        """
        Writes CSV data to CSV folder in root directory of application.
        :param armyTime: Boolean if date will be in army type. If false, data will be in standard type.
        :param totalData: Data to write to CSV file.
        :param fileName: Filename to name CSV in.
        :return: Absolute path to CSV file.
        """
        folderName = 'CSV'
        currentPath = os.getcwd()
        os.chdir(ROOT_DIR)

        if not os.path.exists(folderName):  # Create CSV folder if it doesn't exist
            os.mkdir(folderName)
        os.chdir(folderName)  # Go inside the folder.

        if not os.path.exists(self.symbol):  # Create symbol folder inside CSV folder if it doesn't exist.
            os.mkdir(self.symbol)
        os.chdir(self.symbol)  # Go inside the folder.

        with open(fileName, 'w') as f:
            f.write("Date_UTC, Open, High, Low, Close, Volume, Quote_Asset_Volume, Number_of_Trades, "
                    "Taker_Buy_Base_Asset, Taker_Buy_Quote_Asset\n")
            for data in totalData:
                if armyTime:
                    parsedDate = data['date_utc'].strftime("%m/%d/%Y %H:%M")
                else:
                    parsedDate = data['date_utc'].strftime("%m/%d/%Y %I:%M %p")
                f.write(f'{parsedDate}, {data["open"]}, {data["high"]}, {data["low"]}, {data["close"]}, '
                        f'{data["volume"]}, {data["quote_asset_volume"]}, {data["number_of_trades"]}, '
                        f'{data["taker_buy_base_asset"]}, {data["taker_buy_quote_asset"]}\n')

        path = os.path.join(os.getcwd(), fileName)
        os.chdir(currentPath)

        return path

    def create_csv_file(self, descending: bool = True, armyTime: bool = True) -> str:
        """
        Creates a new CSV file with current interval and returns the absolute path to file.
        :param descending: Boolean that decides where values in CSV are in descending format or not.
        :param armyTime: Boolean that dictates where dates will be written in army-time format or not.
        """
        self.update_database_and_data()  # Update data if updates exist.
        fileName = f'{self.symbol}_data_{self.interval}.csv'
        if descending:
            path = self.write_csv_data(self.data, fileName=fileName, armyTime=armyTime)
        else:
            path = self.write_csv_data(self.data[::-1], fileName=fileName, armyTime=armyTime)

        self.output_message(f'Data saved to {path}.')
        return path

    @staticmethod
    def get_custom_csv_data(symbol: str, interval: str, descending: bool = True) -> str:
        """
        Creates a new CSV file with interval specified and returns the absolute path of CSV file.
        :param symbol: Symbol to get data for.
        :param interval: Interval to get data for.
        :param descending: Returns data in specified sort. If descending, writes data from most recent to oldest data.
        """
        tempData = Data(interval=interval, symbol=symbol)
        return tempData.create_csv_file(descending=descending)

    def is_valid_interval(self, interval: str) -> bool:
        """
        Returns whether interval provided is valid or not.
        :param interval: Interval argument.
        :return: A boolean whether the interval is valid or not.
        """
        availableIntervals = ('12h', '15m', '1d', '1h',
                              '1m', '2h', '30m', '3d', '3m', '4h', '5m', '6h', '8h')
        if interval in availableIntervals:
            return True
        else:
            self.output_message(f'Invalid interval. Available intervals are: \n{availableIntervals}')
            return False

    def is_valid_symbol(self, symbol: str) -> bool:
        """
        Checks whether the symbol provided is valid or not for Binance.
        :param symbol: Symbol to be checked.
        :return: A boolean whether the symbol is valid or not.
        """
        tickers = self.binanceClient.get_all_tickers()
        for ticker in tickers:
            if ticker['symbol'] == symbol:
                return True
        return False

    def is_valid_average_input(self, shift: int, prices: int, extraShift: int = 0) -> bool:
        """
        Checks whether shift, prices, and (optional) extraShift are valid.
        :param shift: Periods from current period.
        :param prices: Amount of prices to iterate over.
        :param extraShift: Extra shift for EMA.
        :return: A boolean whether shift, prices, and extraShift are logical or not.
        """
        if shift < 0:
            self.output_message("Shift cannot be less than 0.")
            return False
        elif prices <= 0:
            self.output_message("Prices cannot be 0 or less than 0.")
            return False
        elif shift + extraShift + prices > len(self.data) + 1:
            self.output_message("Shift + prices period cannot be more than data available.")
            return False
        return True

    def verify_integrity(self) -> bool:
        """
        Verifies integrity of data by checking if there's any repeated data.
        :return: A boolean whether the data contains no repeated data or not.
        """
        if len(self.data) < 1:
            self.output_message("No data found.", 4)
            return False

        previousData = self.data[0]
        for data in self.data[1:]:
            if data['date_utc'] == previousData['date_utc']:
                self.output_message("Repeated data detected.", 4)
                self.output_message(f'Previous data: {previousData}', 4)
                self.output_message(f'Next data: {data}', 4)
                return False
            previousData = data

        self.output_message("Data has been verified to be correct.")
        return True

    def get_summation(self, prices: int, parameter: str, round_value: bool = True, update: bool = True) -> float:
        """
        Returns total summation.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param prices: Amount of periods to iterate through for summation.
        :param parameter: Parameter to iterate through.
        :param round_value: Boolean that determines whether returned output is rounded or not.
        :return: Total summation.
        """
        data = [self.get_current_data()] + self.data if update else self.data
        data = data[:prices]

        total = 0
        for period in data:
            total += period[parameter]

        if round_value:
            return round(total, 0)
        return total

    def get_lowest_low_value(self, prices: int, parameter: str = 'low', round_value: bool = True,
                             update: bool = True) -> float:
        """
        Function that returns the lowest low values.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param prices: Amount of periods to iterate through.
        :param parameter: Parameter to iterate through. By default, it is low.
        :param round_value: Boolean that determines whether returned output is rounded or not.
        :return: Lowest low value from periods.
        """
        data = [self.get_current_data()] + self.data if update else self.data
        data = data[:prices]

        lowest = data[0][parameter]

        for period in data[1:]:
            if period[parameter] < lowest:
                lowest = period[parameter]

        if round_value:
            return round(lowest, 2)
        return lowest

    def get_highest_high_value(self, prices: int, parameter: str = 'high', round_value: bool = True,
                               update: bool = True) -> float:
        """
        Function that returns the highest high values.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param prices: Amount of periods to iterate through.
        :param parameter: Parameter to iterate through. By default, it is high.
        :param round_value: Boolean that determines whether returned output is rounded or not.
        :return: Highest high value from periods.
        """
        data = [self.get_current_data()] + self.data if update else self.data
        data = data[:prices]

        highest = data[0][parameter]

        for period in data[1:]:
            if period[parameter] > highest:
                highest = period[parameter]

        if round_value:
            return round(highest, 2)
        return highest

    @staticmethod
    def helper_get_ema(up_data: list, down_data: list, periods: int) -> tuple:
        """
        Helper function to get the EMA for relative strength index.
        :param down_data: Other data to get EMA of.
        :param up_data: Data to get EMA of.
        :param periods: Number of periods to iterate through.
        :return: EMA
        """
        emaUp = up_data[0]
        emaDown = down_data[0]
        alpha = 1 / periods

        for index in range(1, len(up_data)):
            emaUp = up_data[index] * alpha + emaUp * (1 - alpha)
            emaDown = down_data[index] * alpha + emaDown * (1 - alpha)

        return emaUp, emaDown

    def get_rsi(self, prices: int = 14, parameter: str = 'close', shift: int = 0, round_value: bool = True,
                update: bool = True) -> float:
        """
        Returns relative strength index.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param prices: Amount of prices to iterate through.
        :param parameter: Parameter to use for iterations. By default, it's close.
        :param shift: Amount of prices to shift prices by.
        :param round_value: Boolean that determines whether final value is rounded or not.
        :return: Final relative strength index.
        """
        if not self.is_valid_average_input(shift, prices):
            raise ValueError('Invalid input specified.')

        if shift > 0:
            data = self.data
            shift -= 1
        else:
            data = [self.get_current_data()] + self.data if update else self.data

        start = 500 + prices + shift if len(data) > 500 + prices + shift else len(data)
        data = data[shift:start]
        data = data[:]
        data.reverse()

        ups, downs = get_ups_and_downs(data=data, parameter=parameter)
        averageUp, averageDown = self.helper_get_ema(ups, downs, prices)
        rs = averageUp / averageDown
        rsi = 100 - 100 / (1 + rs)

        if shift == 0:
            self.rsi_data[prices] = rsi

        if round_value:
            return round(rsi, 2)
        return rsi

    def get_sma(self, prices: int, parameter: str, shift: int = 0, round_value: bool = True,
                update: bool = True) -> float:
        """
        Returns the simple moving average with run-time data and prices provided.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param boolean round_value: Boolean that specifies whether return value should be rounded
        :param int prices: Number of values for average
        :param int shift: Prices shifted from current price
        :param str parameter: Parameter to get the average of (e.g. open, close, high or low values)
        :return: SMA
        """
        if not self.is_valid_average_input(shift, prices):
            raise ValueError('Invalid average input specified.')

        data = [self.get_current_data()] + self.data if update else self.data
        data = data[shift: prices + shift]  # Data now starts from shift and goes up to prices + shift
        sma = sum([get_data_from_parameter(data=period, parameter=parameter) for period in data]) / prices

        if round_value:
            return round(sma, 2)
        return sma

    def get_wma(self, prices: int, parameter: str, shift: int = 0, round_value: bool = True,
                update: bool = True) -> float:
        """
        Returns the weighted moving average with run-time data and prices provided.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param shift: Prices shifted from current period.
        :param boolean round_value: Boolean that specifies whether return value should be rounded
        :param int prices: Number of prices to loop over for average
        :param parameter: Parameter to get the average of (e.g. open, close, high or low values)
        :return: WMA
        """
        if not self.is_valid_average_input(shift, prices):
            raise ValueError('Invalid average input specified.')

        data = [self.get_current_data()] + self.data if update else self.data
        total = get_data_from_parameter(data=data[shift], parameter=parameter) * prices
        data = data[shift + 1: prices + shift]  # Data now does not include the first shift period.

        index = 0
        for x in range(prices - 1, 0, -1):
            total += x * get_data_from_parameter(data=data[index], parameter=parameter)
            index += 1

        divisor = prices * (prices + 1) / 2
        wma = total / divisor
        if round_value:
            return round(wma, 2)
        return wma

    def get_ema(self, prices: int, parameter: str, shift: int = 0, sma_prices: int = 5,
                round_value: bool = True, update: bool = True) -> float:
        """
        Returns the exponential moving average with data provided.
        :param update: Boolean for whether function should call API and get latest data or not.
        :param shift: Prices shifted from current period.
        :param round_value: Boolean that specifies whether return value should be rounded
        :param int sma_prices: SMA prices to get first EMA over
        :param int prices: Days to iterate EMA over (or the period)
        :param str parameter: Parameter to get the average of (e.g. open, close, high, or low values)
        :return: EMA
        """
        if not self.is_valid_average_input(shift, prices, sma_prices):
            raise ValueError('Invalid average input specified.')
        elif sma_prices <= 0:
            raise ValueError("Initial amount of SMA values for initial EMA must be greater than 0.")

        ema_data = self.ema_data
        if shift > 0:
            if prices in ema_data and parameter in ema_data[prices] and len(ema_data[prices][parameter]) > 0:
                latestDate = ema_data[prices][parameter][-1][1]
                if self.is_latest_date(latestDate):
                    shift += 1
                    ema = ema_data[prices][parameter][-shift][0]
                    if round_value:
                        return round(ema, 2)
                    return ema

        multiplier = 2 / (prices + 1)

        if prices in ema_data and parameter in ema_data[prices] and len(ema_data[prices][parameter]) > 0:
            latestDate = ema_data[prices][parameter][-1][1]
            if self.is_latest_date(latestDate):
                current_data = self.get_current_data() if update else self.data[0]
                current_price = get_data_from_parameter(data=current_data, parameter=parameter)
                previous_ema = ema_data[prices][parameter][-2][0]
                ema = current_price * multiplier + previous_ema * (1 - multiplier)
                ema_data[prices][parameter][-1] = (round(ema, 2), latestDate)
            else:
                current_data = self.get_current_data() if update else self.data[0]
                current_price = current_data[parameter]
                counter = 1
                for period in self.data[::-1]:
                    if period['date_utc'] == latestDate:
                        break
                    counter += 1

                ema_data[prices][parameter].pop()  # Remove last EMA as it could be invalid.
                for period in self.data[-counter:]:
                    previous_ema = ema_data[prices][parameter][-1][0]
                    ema = period[parameter] * multiplier + previous_ema * (1 - multiplier)
                    ema_data[prices][parameter].append((round(ema, 2), period['date_utc']))

                previous_ema = ema_data[prices][parameter][-1][0]
                ema = current_price * multiplier * previous_ema * (1 - multiplier)  # Set current EMA.
                ema_data[prices][parameter].append((round(ema, 2), current_data['date_utc']))

            if shift > 0:
                ema = ema_data[prices][parameter][-shift][0]
        else:
            data = [self.get_current_data()] + self.data if update else self.data
            sma_shift = len(data) - sma_prices
            ema = self.get_sma(sma_prices, parameter, shift=sma_shift, round_value=False, update=update)
            values = [(round(ema, 2), str(data[sma_shift]['date_utc']))]

            for day in range(len(data) - sma_prices - shift):
                current_index = len(data) - sma_prices - day - 1
                current_price = get_data_from_parameter(data=data[current_index], parameter=parameter)
                ema = current_price * multiplier + ema * (1 - multiplier)
                values.append((round(ema, 2), data[current_index]['date_utc']))

            ema_data[prices] = {parameter: values}

        self.ema_data = ema_data
        if round_value:
            return round(ema, 2)
        return ema

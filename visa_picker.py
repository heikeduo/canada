"""
setup TODO:
1. provide various paths (see Paths)
2. provide controller channel (see channel)

"""
import logging
import os
import configparser
import datetime
import pprint
import ssl
import time
import traceback
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Tuple

from telethon import TelegramClient, events
from selenium import webdriver
from selenium.common import WebDriverException
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.remote.webelement import WebElement

from visa_query import default_driver, now_string, TeleSubscriber, parse_visa_message, VisaMessage, is_signed_in, login


### --- visa module ----
def validate_driver(driver: webdriver.Chrome, logger: Optional[logging.Logger] = None) -> webdriver.Chrome:
    """ call driver.get() to make sure driver is working, sometimes after
    long dormancy, driver will freeze and subsequent calls get "disconnected" error
    if that happens, recreated a driver and return it
    """
    try:
        driver.get('http://httpbin.org/get?show_env=1')
        time.sleep(0.1)
        txt = driver.find_element(By.XPATH, '//body')
        if logger:
            logger.info('validated driver')
        return driver
    except WebDriverException as e:
        # traceback.print_exc()
        (logger or logging.getLogger('validate_dr')).error(
            'try httpbin.org failed! Traceback: \n' + traceback.format_exc())
        if logger is not None:
            logger.warn('driver freezed, re-create a driver using driver.init_args')

        init_args = driver.init_args
        dr = webdriver.Chrome(service=Service(init_args['path']), options=init_args['options'])
        dr.get('http://httpbin.org/get?show_env=1')
        time.sleep(0.1)
        dr.set_window_size(1100, 1000)
        if logger is not None:
            logger.info(f'driver successfully created using init_args {init_args}')
        return dr

### --- reschedule module
BAD_CITIES = ['Halifax', 'Quebec City']

DateOnlyScorer = Callable[[str], float]
DateCityScorer = Callable[[str, str], float]   # dateStr, city

def to_iso_date(td: WebElement):
    """ note to remember zero padding for month and day values"""
    return '{}-{:02d}-{:02d}'.format(
        td.get_attribute('data-year'),
        int(td.get_attribute('data-month')) + 1,
        int(td.text))

def go_to_appointment(driver):
    if not driver.title.startswith('Groups'):
        # this will land you in group page
        driver.get('https://ais.usvisa-info.com/en-ca/niv/account')
    e = driver.find_element(By.LINK_TEXT, 'Continue')  # continue button element contains scheduleId
    parts = e.get_attribute('href').split('/')
    scheduleId = parts[parts.index('schedule') + 1]
    driver.get(f'https://ais.usvisa-info.com/en-ca/niv/schedule/{scheduleId}/appointment')

@dataclass
class ApptInfo:
    city: str
    date: str  # iso-format
    time: str  # hh:ss
    status: str = 'Attend Appointment'

@dataclass
class Picker:
    user: str
    pswd: str
    driver: Chrome
    maxPages: int
    screenshotPath: str
    cityScorer: DateOnlyScorer
    countryScorer: Optional[DateCityScorer] = None

    # strategy
    stopOnBest: bool = False
    dryRun: bool = False
    checkTargetFirst = True

    logger = logging.getLogger('Picker')
    isSetup: bool = field(init=False, default=False)
    status: ApptInfo = field(init=False, default=None)
    counter: int = field(init=False, default=0)
    loginCount: int = field(init=False, default=0)

    def setup(self):
        if not is_signed_in(self.driver):
            self.logger.info(f'{self.user} logging in')
            self.loginCount += 1
            login(self.driver, self.user, self.pswd)
        # go to appt page from somewhere else
        assert self.driver.title.startswith('Groups')
        self.status = self.check_current_booked()
        go_to_appointment(self.driver)
        self.isSetup = True

    def reset_no_logout(self):
        self.driver.get('https://ais.usvisa-info.com/en-ca/niv/account')

    def check_current_booked(self):
        driver: Chrome = self.driver
        status = driver.find_element(By.XPATH, '//h4[@class="status"]').text[len('Current Status'):].strip()
        els = driver.find_elements(By.XPATH, '//p[@class="consular-appt"]')
        if els:
            apptInfo = els[0].text.split(':', 1)[1]
            monthDay, year, rest = apptInfo.split(',', 2)
            dateStr = ', '.join([monthDay, year]).strip()
            isoDate = datetime.datetime.strptime(dateStr, '%d %B, %Y').date().isoformat()
            isoTime, rest = rest.strip().split(' ', 1)
            rest: str = rest
            city = rest[:rest.find('local time')].strip()
        else:
            # have not yet scheduled anything ...
            city, isoDate, isoTime = '', '', ''
        return ApptInfo(city=city, date=isoDate, time=isoTime, status=status)

    def pick_by_city(self):
        self.logger.info(f'{self.user} current status: {self.status_info()}; login count: {self.loginCount}')
        self.logger.info(f'start pick #{self.counter} ')
        self.goodDates = allGoodDates = {}
        # step 1, search best options per city
        cities = self.driver.find_elements(By.XPATH,
                                           '//*[@id="appointments_consulate_appointment_facility_id"]/option[text()]')
        sel = Select(self.driver.find_element(By.XPATH, '//*[@id="appointments_consulate_appointment_facility_id"]'))
        sel.select_by_value('')  # to reset
        time.sleep(0.05)
        for option in cities:
            city = option.text
            if city in BAD_CITIES:
                continue
            self.logger.info(f'explore {city}')
            self.click_on_city(city)
            time.sleep(0.5)  # takes time to get response
            if self.has_no_slots():
                self.logger.info(f'no slots {city}, continue')
                continue
            self.open_calendar()
            firstDates: List[str] = self.find_first_n_dates(n=20, maxPages=self.maxPages)
            self.click_on_empty()
            self.logger.info(f'{city} first few slots {firstDates}')
            goodDates = self.choose_good_dates_in_order(firstDates)  # filter irrelevant
            if not goodDates:
                continue
            allGoodDates[city] = goodDates

        # flatten results from all cities into one bigger list (city step score is discarded)
        options: List[Tuple[str, str]] = [(city, d) for city, dates in allGoodDates.items() for _score, d in dates]
        self.logger.info(f'options: {options}')

        # step 2. use country scorer to sort all options, to give "action" items
        actions = self.choose_best_options_in_order(options)
        # actions list of (score, date, city) reason city order is alphabetical and has no sense
        self.logger.info(f'actions: {actions}')
        self.counter += 1
        return actions

    def pick_target(self, target: ApptInfo, tryOthers=False):
        self.logger.info(f'{self.user} current status: {self.status_info()}; login count: {self.loginCount}')
        self.logger.info(f'start a targeted pick #{self.counter} for {target.city} on {target.date}')
        if self.checkTargetFirst:
            self.logger.info(f'checkTargetFirst=True, compare target ({target.city}, {target.date}) with '
                             f'current slot ({self.status.city}, {self.status.date}, {self.status.time})')
            targetScore, currScore = self.countryScorer(target.date, target.city), self.countryScorer(self.status.date,
                                                                                                      self.status.city)
            if targetScore < currScore:
                self.logger.info(f"target's core is less than existing: {targetScore} < {currScore}, return no actions")
                return []
        time.sleep(0.05)
        sel = Select(self.driver.find_element(By.XPATH, '//*[@id="appointments_consulate_appointment_facility_id"]'))
        sel.select_by_visible_text('')  # to reset
        time.sleep(0.05)
        city = target.city
        if not any(target.city == opt.text for opt in sel.options):
            self.logger.info(f'unknown city {target.city}')
            raise ValueError('unknown facility city')
        sel.select_by_visible_text(target.city)
        self.logger.info(f'explore {city}')
        time.sleep(0.05)  # takes time to get response
        if self.has_no_slots():
            self.logger.info(f'no slots {city}, continue')
            if tryOthers:
                return self.pick_by_city()
            return []

        self.open_calendar()
        firstDates: List[str] = self.find_first_n_dates(n=20, maxPages=self.maxPages)
        self.click_on_empty()
        self.logger.info(f'{city} first few slots {firstDates}')
        goodDates = self.choose_good_dates_in_order(firstDates)  # filter irrelevant
        options = [(city, d) for _score, d in goodDates]
        actions = self.choose_best_options_in_order(options)
        # actions list of (score, date, city) reason city order is alphabetical and has no sense
        self.logger.info(f'actions: {actions}')
        self.counter += 1
        return actions

    def act_on_best_options(self, options: List[Tuple[float, str, str]]):
        self.logger.info('act on the options {}'.format(options))
        assert self.driver.title.startswith('Schedule App'), 'not on the appointment page'
        for score, d, city in options:
            self.click_on_city(city)
            time.sleep(0.5)  # takes time to get response, hard to wait on certain item
            self.open_calendar()

            self.go_to_calendar_begin()  # this is because the following logic only goes forward to search
            el = self.get_date_in_calender(score, d, city)
            if el:
                el.click()
                self.logger.info(f'  select date {d} @ {city}')
                time.sleep(0.2)
                self.click_time(1)  # simplified: just use the first available slot
                sel = Select(self.driver.find_element(By.ID, 'appointments_consulate_appointment_time'))
                isoTime = sel.all_selected_options[0].text
                act: Optional[str] = self.on_found_slot(city, d, isoTime)
                # self.logger.info('done')
                if act is not None:
                    yield d, city, act
                if act is not None or self.stopOnBest:
                    return
            else:
                print(f'could not select {city} on {d}')

    def better_than_or_eq(self, city, dateStr):
        if not self.status or not self.status.date:
            return False  # has no appt yet, always false
        return self.countryScorer(self.status.date, self.status.city) >= self.countryScorer(dateStr, city)

    def current_score(self):
        if not self.isSetup:
            self.setup()
        return self.countryScorer(self.status.date, self.status.city) if self.status.date else -2

    def status_info(self) -> str:
        """ for logging """
        if self.status.status != 'Attend Appointment':
            return 'not booked'
        return f'{self.status.city} on {self.status.date}, score = {self.current_score()}'

    def on_found_slot(self, city, d, time) -> Optional[str]:
        """ if have anything valuable, confirms, takes shot, returns the screenshot file,
            otherwise, return None
        """
        if self.status and self.status.status == 'Attend Appointment':
            self.logger.info(f'compare this found ({city}, {d}, {time}) with '
                             f'current slot ({self.status.city}, {self.status.date}, {self.status.time})')
            thisScore, currScore = self.countryScorer(d, city), self.countryScorer(self.status.date, self.status.city)
            if thisScore > currScore:
                self.logger.info(f'this spot is better than existing: {thisScore} > {currScore}, try book it')
                pass
            else:
                self.logger.info(f'this spot is NOT better than existing: {thisScore} <= {currScore}, skip')
                return None
        screenShot = f'{city}_{d}_at_{self.get_timestamp_str()}.png'
        self.save_screenshot(screenShot)
        if self.dryRun:
            self.logger.info('dry run, skip confirm..')
        else:
            try:
                self.confirm()
            except:
                self.logger.error(f'confirm {city} {d} failed!')
            else:
                self.logger.info(f'Successfully confirmed! {city} {d}')
                # Note! set status here is important to let other options in the same pick cycle to know
                # of the change of status...
                self.status = ApptInfo(city, d, time)  # default status is booked...
                self.logger.info(f'{self.user} status changed: {self.status_info()}')
        return os.path.join(self.screenshotPath, screenShot)  # yield successful operation

    def get_date_in_calender(self, score, d, city) -> Optional[WebElement]:
        def look_through_batch(dates) -> Tuple[int, Optional[WebElement]]:
            for el in dates:
                if to_iso_date(el) > d:
                    # it was there, or incorrect information
                    print(f'reached to {to_iso_date(el)} before designed target date {d}, abort')
                    return 2, None  # reach over, bad state
                elif to_iso_date(el) == d:
                    # found it
                    return 1, el
            return 0, None

        for i in range(self.maxPages + 2):  # +2 to prevent not reaching previously reached position
            time.sleep(0.1)
            batchDates: List[WebElement] = self.get_current_page_slots()
            # print(f'page {i}')
            # print('batchDates found: ', [to_iso_date(el) for el in batchDates])
            if len(batchDates) == 0:
                self.click_next()
                continue
            found, el = look_through_batch(batchDates)
            if found >= 1:
                if found == 1:
                    # print(f'land in option ({score}, {d}, {city}), choose time')
                    return el
                else:
                    print(f'did not find, break')
                    return None
            self.click_next()
        return None

    def choose_best_options_in_order(self, options: List[Tuple[str, str]]) -> List[Tuple[float, str, str]]:
        """ select the best from all spots found from all cities, an option is (city, dateStr)
        """
        if not self.countryScorer:
            return []
        scoreTable = [(self.countryScorer(d, city), d, city) for city, d in options]
        scoreTable = sorted([(-score, d, city) for score, d, city in scoreTable if score > 0])
        return [(-sc, d, city) for sc, d, city in scoreTable]

    def find_first_n_dates(self, n=5, maxPages=13):
        out = set()
        for i in range(maxPages // 2 + 1):
            batchDates: List[
                WebElement] = self.get_current_page_slots()  # self.driver.find_elements(By.XPATH, '//td[@data-event="click"]')
            if len(batchDates) > 0:
                out.update(set(to_iso_date(el) for el in batchDates))
                if len(out) >= n:  # len() does work on a set
                    break
            self.click_next()  # self.driver.find_element(By.XPATH, '//*[@id="ui-datepicker-div"]//a[@data-handler="next"]').click()
            time.sleep(0.05)
            self.click_next()
            time.sleep(0.05)
        return sorted(out)[:n]

    def has_no_slots(self):
        # 'display: none;' for good,  'display: block;' is bad
        return 'block' in self.driver.find_element(By.ID, 'consulate_date_time_not_available').get_attribute('style')

    def choose_good_dates_in_order(self, dates: List[str]):
        # datetime.date.fromisoformat() py >= 3.7
        scoreTables = [(self.cityScorer(d), d) for d in dates]
        scoreTables = sorted([(-score, d) for score, d in scoreTables if score > 0])
        return [(-score, d) for score, d in scoreTables]  # invert back

    def click_on_city(self, cityName):
        assert self.driver.current_url.endswith('appointment'), "cannot do this not on appointment page"
        self.driver.find_element(By.XPATH,
                                 f'//*[@id="appointments_consulate_appointment_facility_id"]/option[text()="{cityName}"]').click()

    def click_on_empty(self):
        # self.driver.find_element(By.XPATH, "//legend").click()  # simulate click on blank area to clear data-picker
        self.driver.find_element(By.XPATH,
                                 '//*[@id="appointments_consulate_address"]').click()  # on linux, legend is not clickable

    def open_calendar(self):
        # assert
        self.click_on_empty()  # click on address, datepicker disappears is automatic, not network
        time.sleep(0.01)
        self.driver.find_element(By.XPATH, '//*[@id="appointments_consulate_appointment_date_input"]').click()

    def click_next(self):
        # todo: assert calendar open,
        # ?? should open calendar if not?? I'd prefer not add it here, causing logical error
        self.driver.find_element(By.XPATH, '//*[@id="ui-datepicker-div"]//a[@data-handler="next"]').click()

    def go_to_calendar_begin(self):
        prevs = self.driver.find_elements(By.XPATH, '//*[@id="ui-datepicker-div"]//a[@data-handler="prev"]')
        while prevs:
            prevs[0].click()
            time.sleep(0.05)
            prevs = self.driver.find_elements(By.XPATH, '//*[@id="ui-datepicker-div"]//a[@data-handler="prev"]')

    def click_prev(self):
        self.driver.find_element(By.XPATH, '//*[@id="ui-datepicker-div"]//a[@data-handler="prev"]').click()

    def get_current_page_slots(self) -> List[WebElement]:
        return self.driver.find_elements(By.XPATH, '//td[@data-event="click"]')

    def get_banner_months(self):
        frontMonth = self.driver.find_element(By.XPATH, '//*[@id="ui-datepicker-div"]/div[1]/div/div').text
        backMonth = self.driver.find_element(By.XPATH, '//*[@id="ui-datepicker-div"]/div[2]/div/div').text
        return frontMonth, backMonth

    def get_times(self):
        # non-empty times
        return [e.text for e in self.driver.find_elements(By.XPATH,
                                                          '//*[@id="appointments_consulate_appointment_time"]/option[text()!=""]')]

    def click_time(self, num):
        self.driver.find_element(By.XPATH,
                                 f'//*[@id="appointments_consulate_appointment_time"]/option[{num + 1}]').click()

    def save_screenshot(self, name=None):
        if name is None:
            name = datetime.datetime.now().isoformat().replace(':', '-').replace('.', '_') + '.png'
        fileName = os.path.join(self.screenshotPath, name)
        self.driver.save_screenshot(fileName)
        self.logger.info(f'saved to {fileName}')

    def confirm(self):
        # //*[@id="appointments_submit"]
        el = self.driver.find_element(By.ID, 'appointments_submit')
        el.click()
        time.sleep(0.5)
        self.logger.info('clicked submit')
        confirms = self.driver.find_elements(By.LINK_TEXT, 'Confirm')  # find_element() will trigger exception
        if confirms:
            confirms[0].click()
            self.logger.info('clicked confirm')
        self.driver.find_element(By.XPATH, '//button[text()="No Thanks"]').click()
        self.logger.info('No Thanks lol')

    def get_timestamp_str(self):
        # return datetime.datetime.now().isoformat().replace(':', '-').replace('.', '_')
        return now_string()



class Paths:
    log = 'logs/tele3.log'
    screenshot = '<path to screenshot>'
    teleClient = "<path to client credential ini"
    teleSession = 'sessions/sender.session'


@dataclass
class Sensor(TeleSubscriber):
    pickers: List[Picker]
    # client: TelegramClient

    maxRun: int = -1  # on visa message...

    visaMsgCounter: int = field(init=False, default=0)
    pickerCounter: int = field(init=False, default=0)

    controlChannel = 'Channel:<some channel>'
    disconnectMsg = 'disconnect'

    def take_snapshot(self, message: str):
        i = message.split()[1]
        try:
            i = int(i)
        except ValueError:
            i = 0
        i = 0 if i >= len(self.pickers) or i < 0 else i
        _logger.info(f'taking a snapshot for picker {i} on demand by instruction {message}')
        picker = self.pickers[i]
        picker.driver.save_screenshot(f'{Paths.screenshot}/OnDemand_sensor_{now_string()}.png')
        return

    def handle_chat_message(self, message: str, chatTag: str, dt: datetime.datetime):
        if chatTag == self.controlChannel:
            if message.lower().startswith('snapshot'):
                self.take_snapshot(message)
                return
            elif message.lower().startswith(self.disconnectMsg):
                _logger.info(f'received disconnect message "{message}", disconnect tele client')
                # this could gracefully complete and return if run inside a Jupyter notebook cell.
                self.unsubscribe()
                self.client.disconnect()
                return
            ## any more functionality could be added here.

        if 'H:' not in message:
            return
        # it's then considered a visa message
        visaMsg = parse_visa_message(message, dt)
        _logger.info(f'visa info: parsed message {visaMsg}')

        if visaMsg.city in ['Vancouver', 'Calgary', 'Ottawa', 'Toronto', 'Montreal']:
            _logger.info('city valid, on_visa_message')
            self.on_useful_visa_msg(visaMsg)

    def on_useful_visa_msg(self, visaMsg: VisaMessage):
        """ when visa info is one of the interesting cities"""
        self.visaMsgCounter += 1
        if visaMsg.date >= '2023-11-01' or visaMsg.date <= '2023-02-14':
            # instead of shortcircuit, validate driver
            for picker in self.pickers:
                validate_driver(picker.driver, logger=_logger)
            return
        # then will trigger
        lowestScore = self.pickers[0].current_score()  # will call setup if not yet
        targetScore = self.pickers[0].countryScorer(visaMsg.date,
                                                    visaMsg.city)  # well, this is using pickers[0]'s scorer, but good enough
        # if self.pickers[0].better_than_or_eq(visaMsg.city, visaMsg.date):
        if lowestScore >= targetScore:
            _logger.info(
                f'pickers[0] {self.pickers[0].user} score {lowestScore} >= targetScore ({targetScore}) for ({visaMsg.city}, {visaMsg.date}), '
                f'skip pickers')
            return
        else:
            # _logger.info(f'{visaMsg.city} has {visaMsg.date}, should fire pickers')
            _logger.warning(
                f'pickers[0] {self.pickers[0].user} score {lowestScore} < targetScore ({targetScore}) for ({visaMsg.city}, {visaMsg.date}), '
                f'fire pickers')
        try:
            self.fire_pickers(self.pickers, target=ApptInfo(visaMsg.city, visaMsg.date, '', ''))
        finally:
            if self.maxRun > 0 and self.visaMsgCounter >= self.maxRun:
                _logger.info(f'has executed on_visa_message {self.visaMsgCounter} times, disconnect the client')
                self.client.disconnect()

    def fire_pickers(self, pickers: List[Picker], target: Optional[ApptInfo] = None):

        t1 = time.time()
        for j, picker in enumerate(pickers):
            # if picker.better_than_or_eq(d, city):
            #     continue
            # actually, we should fire pickers whenever the date is promising
            try:
                picker.setup()
                # _logger.info(f'{picker.user} has appt info as {picker.status}, current score: {picker.current_score()}')
                if target is not None:
                    actions = picker.pick_target(target, tryOthers=(j > 0))
                else:
                    actions = picker.pick_by_city()
                if actions:
                    _logger.info(f'{picker.user} taking actions on {actions}')
                    results = list(picker.act_on_best_options(actions))
                    _logger.info(f'results: {results}')
                    # on_results(picker, results)
                    self.on_results(results)

                    if j + 1 < len(pickers) and all(
                            pickers[j + 1].better_than_or_eq(city, d) for _, d, city in actions):
                        _logger.info(f'next picker {pickers[j + 1].user} better than all these options, skip')
                        break
                else:
                    break  # if no actions, should skip as well..
            except Exception as e:
                picker.driver.save_screenshot(
                    f'{Paths.screenshot}/Exception_sensor_{now_string()}.png')
                traceback.print_exc()
                picker.reset_no_logout()
                raise e

        pickers.sort(key=lambda
            p: p.current_score())  # always keep the lowest score first, b/c high scores do not need to activate sometimes..

        self.pickerCounter += 1
        _logger.info(f'{self.pickerCounter}-th picker run finishes in {time.time() - t1:.1f} seconds')
        for picker in pickers:
            _logger.info(f'{picker.user} {picker.status_info()}')
        return pickers

    def on_results(self, results):
        if not results:
            return
        _logger.info(f'send email with images for {picker.user}')
        # send emails...
        _logger.info('email sent')


if __name__ == '__main__':
    """
    # Example ini file content 
    
    [Telegram]
    # no need for quotes
    
    # you can get telegram development credentials in telegram API Development Tools
    api_id = some_id
    api_hash = some_api_key
    
    # use full phone number including + and country code
    phone = +12345678901
    
    """

    runCfg = dict(
        maxRun=-1,   # set a pos number to only run that number times, for debug
        logFile=Paths.log,
    )

    logging.basicConfig(filename=runCfg['logFile'],
                        format='%(levelname)7s %(asctime)s %(name)-15s %(message)s', level='INFO')
    _logger = logging.getLogger('jupyter-tele')


    config = configparser.ConfigParser()
    config.read(Paths.teleClient)
    client = TelegramClient(Paths.teleClient, config['Telegram']['api_id'], config['Telegram']['api_hash'])

    DATE_SCORES = {  # (]
        '2023-02-19': -1,
        '2023-03-01': 2,

        '2023-03-24': 25,
        '2023-03-31': 40,
        '2023-04-04': 25,
        '2023-09-01': -1
    }


    def city_score(d: str):
        for cutoff, score in DATE_SCORES.items():
            if cutoff >= d:
                return score
        return -1


    def country_score(d, city):
        if city in ['Halifax', 'Quebec City', ]:
            return -1
        score = city_score(d)

        if city == 'Calgary':
            if '2023-03-29' <= d <= '2023-03-30':
                return score * 1.1
            if '2023-03-27' <= d < '2023-04-01':
                return score * 1.05
        else:
            # 02-15 other city doesn't deserve another pick...
            if score >= 25:
                return 25
            # if '2023-03-25' <= d <= '2023-04-02':
            #     return score * 1.04
        # if city in ['Calgary', 'Vancouver'] and d <= '2023-03-31':
        #     return score * 1.02
        return score


    pickers = [
        Picker('username', 'some_password', default_driver(), maxPages=13,
               screenshotPath=Paths.screenshot,
               cityScorer=city_score,
               countryScorer=country_score),
    ]

    sensor = Sensor(client=client, pickers=pickers, maxRun=runCfg['maxRun'])
    client.add_event_handler(sensor.handle_event, events.NewMessage)
    # client.remove_event_handler(sensor.handle_event, events.NewMessage)

    SANITY_CHECKS = ['2023-02-08', '2023-02-20', '2023-03-10', '2023-04-06', '2023-05-20', '2023-07-13', '2023-11-30',
                     '2024-01-23']

    for picker in pickers:
        cityScorerChecks = {d: picker.cityScorer(d) for d in SANITY_CHECKS}
        _logger.info(f'sanity check: {picker.user} cityScorer: \n{pprint.pformat(cityScorerChecks)}')
        countryScorerChecks = {('Vancouver', d): picker.countryScorer(d, 'Vancouver') for d in SANITY_CHECKS}
        _logger.info(f'sanity check (Vancouver): {picker.user} countryScorer: \n{pprint.pformat(countryScorerChecks)}')

        countryScorerChecks = {('Calgary', d): picker.countryScorer(d, 'Calgary') for d in SANITY_CHECKS}
        _logger.info(f'sanity check (Calgary): {picker.user} countryScorer: \n{pprint.pformat(countryScorerChecks)}')

    async with client:
        await client.run_until_disconnected()
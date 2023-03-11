""" put all code in one script

setup todo:
1. tele credentials
2. provide channel as controller
3. provide a few query account

"""

import asyncio
import collections
import configparser
from typing import Union, Dict, Deque
from dataclasses import dataclass, field
import logging
import os

import datetime
import random
import ssl
import time
import traceback
from typing import Callable, List, Optional

import pandas as pd
from selenium.common import WebDriverException
from telethon import TelegramClient, events
from selenium import webdriver
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.wait import WebDriverWait as Wait


_logger = logging.getLogger()

DRIVER_PATH = '<driver path>'
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"

### ---- visa module ----
def get_driver(path, headless=False, userAgent=None, proxy=None):
    """ since selenium 4.0 use executable-path is deprecated """
    chromeOptions = Options()
    # chromeOptions.headless = True  # deprecated way
    if userAgent:
        chromeOptions.add_argument('--user-agent=%s' % userAgent)
    if proxy:
        chromeOptions.add_argument('--proxy-server=%s' % proxy)
    if headless:
        chromeOptions.add_argument('--headless')
    dr = webdriver.Chrome(service=Service(path), options=chromeOptions)
    dr.init_args = {'path': path, 'options': chromeOptions}
    return dr

def login(driver, username, password):
    """ use wait and ec """
    driver.get('https://ais.usvisa-info.com/en-ca/niv/users/sign_in')  # otherwise, it jumps to a jd corporate page.
    Wait(driver, 10).until(EC.presence_of_element_located((By.ID, "user_email"))).send_keys(username)
    time.sleep(0.2)
    driver.find_element(By.ID, "user_password").send_keys(password)
    time.sleep(0.2)
    driver.find_element(By.XPATH, '//div[contains(@class, "icheckbox")]').click()
    time.sleep(0.2)
    driver.find_element(By.NAME, 'commit').click()
    Wait(driver, 2).until(EC.presence_of_element_located((By.LINK_TEXT, 'Continue')))
    logging.info('landed in Groups page')


### ---- reschedule module ----
def is_signed_in(driver):
    driver.get('https://ais.usvisa-info.com/en-ca/niv/account')
    time.sleep(0.5)
    Wait(driver, 5).until(EC.presence_of_element_located((By.LINK_TEXT, 'Change Country')))
    # Wait(driver, 5).until(EC.presence_of_element_located((By.XPATH, '//div[@class="text" and text()="Groups"]')))
    return driver.title.startswith('Groups')  # otherwise is in sign_in page..


### ---- sensor module ----
def default_driver(proxy=None):
    driver = get_driver(DRIVER_PATH,
                      headless=True, proxy=proxy,
                      userAgent=USER_AGENT)
    driver.set_window_size(1100, 1000)   # (1100, 1000) so that the bottom banner on appt page with user info can be seen...
    return driver


### -----  tele module ----
@dataclass
class VisaMessage:
    city: str
    fromDate: str  # in standard isoformat yyyy-mm-dd
    date: str

    updateTime: Optional[datetime.datetime]

def parse_tele_date(d: str):
    """ / or '2/28' or '2024/1/4' """
    if d.strip() == '/':
        return ''
    parts = d.split('/')
    if len(parts) == 3:
        return datetime.datetime.strptime(d, '%Y/%m/%d').date().isoformat()
    elif len(parts) == 2:
        return datetime.datetime.strptime(
            str(datetime.date.today().year) + '/' + d, '%Y/%m/%d').date().isoformat()
    raise ValueError(f'unknown format {d}')

def parse_visa_message(msg: str, updateTime=None) -> VisaMessage:
    """ the likes of 'London H: 2/28 -> 2/27' or '/ -> 2/1'
        'Ottawa H: 2024/1/4 -> 11/14'
    """
    city, rest = msg.split('H:')
    city = city.strip()
    fromStr, toStr = rest.strip().split('->')
    fromStr, toStr = fromStr.strip(), toStr.strip()
    return VisaMessage(city, parse_tele_date(fromStr), parse_tele_date(toStr), updateTime=updateTime)


def go_to_payment_page(driver: Chrome):
    el = driver.find_element(By.LINK_TEXT, 'Continue')
    userId = el.get_attribute('href').split('/')[-2]
    driver.get(f'https://ais.usvisa-info.com/en-ca/niv/schedule/{userId}/payment')
    assert driver.title.startswith('Payment |')

@dataclass
class TeleSubscriber:
    client: TelegramClient

    async def handle_event(self, event):
        """ a generic NewMessage event handler that unpack some info.
        this is to be added to added::

            client.add_event_handler(sensor.handle_event, events.NewMessage)

        """
        chat, msg = event.chat, event.message
        # sloppy version of this, but allows testing much easily
        chatTag = type(chat).__name__ + ':' + (chat.title if hasattr(chat, 'title') else chat.username)
        dt: datetime.datetime = msg.date.astimezone().replace(
            tzinfo=None)  # the replace() call converts "aware" dt to "naive"
        _logger.info(f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {chatTag} from chat{msg.peer_id}: {event.message.message}")
        lag = datetime.datetime.now() - dt
        if lag > datetime.timedelta(minutes=1):
            _logger.info(f'stale msg with lag={lag}, ignore')
            return
        _logger.info(f'lag={lag}, process')
        self.handle_chat_message(msg.message, chatTag, dt)

    def handle_chat_message(self, message: str, chatUser: str, dt: datetime.datetime):
        pass

    def unsubscribe(self):
        _logger.info('unsubscribe')
        self.client.remove_event_handler(self.handle_event)


#### ------ tele unpaid module --------
def get_earliest_slots(driver: Chrome):
    """ assuming already on payment page"""
    tbl = driver.find_element(By.CSS_SELECTOR, '#paymentOptions > div.medium-3.column > table')
    trs = list(tbl.find_elements(By.XPATH, './/tr'))
    data = [
        [x.get_attribute('textContent').strip() for x in tr.find_elements(By.XPATH, './td')]
        for tr in trs]
    return data


def parse_date(x):
    if not isinstance(x, str): return x
    if x == 'No Appointments Available':
        return '-'
    return pd.Timestamp(x).date().isoformat()

def now_string():
    return datetime.datetime.now().isoformat().replace(':', '-').replace('.', '_')

@dataclass
class QueryBase:
    user: str
    pswd: str
    driver: Chrome

    logger = logging.getLogger('QueryBase')
    loginCount: int = field(init=False, default=0)

    def setup(self):
        if not is_signed_in(self.driver):
            self.logger.info(f'{self.user} logging in')
            self.loginCount += 1
            login(self.driver, self.user, self.pswd)
        # go to appt page from somewhere else
        assert self.driver.title.startswith('Groups')

    async def handle_event(self, event):
        """ this is to be added to added
            client.add_event_handler(sensor.handle_event)
        """
        chat, msg = event.chat, event.message
        # sloppy version of this, but allows testing much easily
        chatTag = type(chat).__name__ + ':' + (chat.title if hasattr(chat, 'title') else chat.username)
        dt: datetime.datetime = msg.date.astimezone()
        _logger.info(f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {chatTag} from chat{msg.peer_id}: {event.message.message}")
        if 'H:' not in msg.message:
            return

        # _logger.info('it is a visa info message')
        visaMsg = parse_visa_message(msg.message, dt)
        _logger.info(f'visa info: parsed message {visaMsg}')

        if visaMsg.city in ['Vancouver', 'Calgary', 'Ottawa', 'Toronto', 'Montreal']:
            _logger.info('city valid, on_visa_message')
            self.on_visa_message(visaMsg)

    def on_visa_message(self, visaMsg: VisaMessage):
        pass

    def snapshot(self, snapshotFolder, prefix='OnDemand'):
        self.driver.save_screenshot(f'{snapshotFolder}/{prefix}_{now_string()}.png')

@dataclass
class UnpaidQuery(QueryBase):
    logger = logging.getLogger('UnpaidQuery')

    def query(self):
        go_to_payment_page(self.driver)
        data = get_earliest_slots(self.driver)
        data = {city: parse_date(x) for city, x in data}
        return {city: d for city, d in data.items() if d != '-'}

    def on_visa_message(self, visaMsg: VisaMessage):
        self.setup()
        data = self.query()
        self.logger.info(f'current earliest slots: {data}')


#### ------ query module ------
class Job:
    """ interface """

    def init(self):
        pass

    def run(self, i) -> bool:
        """ return value if eval to True, signals the controller that the loop should be stopped """
        pass

    def close(self):
        pass


class AsyncJob:
    async def init(self):
        pass

    async def run(self, i) -> bool:
        """ return value if eval to True, signals the controller that the loop should be stopped """
        pass

    async def close(self):
        pass


@dataclass
class SleepParam:
    min: int
    max: int

    def get_random_delay(self) -> float:
        return self.min + random.random() * (self.max - self.min)


@dataclass
class Controller(TeleSubscriber):
    """ split the async main logic out:
            connect-client, sub/unsub try-finally, for-loop, timing/async.sleep to yield (remember, this is coro!)

    """
    sleepParam: SleepParam

    running: bool = field(init=False, default=True)  # state var

    controlChannel = 'Channel:<channel name>'
    stopMessage = 'stop async'

    # override
    def handle_chat_message(self, message: str, chatUser: str, dt: datetime.datetime):
        if chatUser == self.controlChannel:
            if message.lower().startswith(self.stopMessage):
                _logger.info('set running to False')
                self.running = False
                # self.client.disconnect()

    async def run_job(self, job: Union[Job, AsyncJob], maxRun: int):
        # 1. start client if not already...
        if not self.client.is_connected():
            _logger.info('client not connected, try connecting')
            await self.client.connect()

        _logger.info('add self.handle_event')
        self.client.add_event_handler(self.handle_event, events.NewMessage)

        try:
            if isinstance(job, Job):
                job.init()
            elif isinstance(job, AsyncJob):
                await job.init()

            for i in range(maxRun):
                # if not self.running:
                #     _logger.info(f'running is False, break')
                #     break
                t0 = time.time()
                _logger.info(f'start {i}-th')
                shouldStop = False
                if isinstance(job, Job):
                    shouldStop = job.run(i)
                elif isinstance(job, AsyncJob):
                    shouldStop = await job.run(i)
                if shouldStop:
                    _logger.info(f'{i}-th run get shouldStop = True, stop')
                    break
                dt = time.time() - t0
                nextWake = self.sleepParam.get_random_delay()
                delay = max(nextWake - dt, 3.)
                _logger.info(f'{i}-th run used {dt:.2f}s, delay {delay:.2f}s')
                # await asyncio.sleep(delay)
                if not await self.wait_and_check_flag(delay, every=0.2):
                    _logger.info(f'running is False, break')
                    break
        # could add except clause to catch and not stop

        finally:
            _logger.info('unsub self.handle_event')
            unsubbed = self.client.remove_event_handler(self.handle_event)
            _logger.info(f'unsub {unsubbed}')
            if isinstance(job, Job):
                job.close()
            elif isinstance(job, AsyncJob):
                await job.close()

    async def wait_and_check_flag(self, delay: float, every=0.5) -> bool:
        """ break down the total wait time to `every`, each time check self.running and return True """
        t0 = time.time()
        T = t0 + delay  # wake-up time
        while t0 < T:
            await asyncio.sleep(min(every, T - t0))
            if not self.running:
                return False
            t0 = time.time()
        return self.running


def format_h_date(iso: str) -> str:
    """ format 2024-01-05 to 2024/1/5, 2023-09-22 to 9/22"""
    if iso == '/':
        return '/'
    d = datetime.date.fromisoformat(iso)
    if d.year == datetime.date.today().year:
        return f'{d.month}/{d.day}'
    return f'{d.year}/{d.month}/{d.day}'


@dataclass
class QueryJob(AsyncJob):
    workers: List[UnpaidQuery]
    client: TelegramClient
    scorer: Callable[[str, str], float]  # (city, date) -> score. > 0 means good, should act
    snapshotFolder: str
    stateInterval: datetime.timedelta = datetime.timedelta(hours=2)

    # strategy variable
    stopWhenAllBlocked = True

    @dataclass
    class WorkerState:
        count: int = 0
        totalCount: int = 0
        # the num of trigger times in interval...
        times: Deque[datetime.datetime] = field(repr=False, default_factory=collections.deque)

        blocked: bool = False
        nextTryTime: datetime.datetime = field(init=False, default=None)

        defaultInterval = datetime.timedelta(hours=6)
        coolDown: datetime.timedelta = datetime.timedelta(hours=4)

        def upd(self, t: datetime.datetime, interval=defaultInterval):
            while self.times and t - self.times[0] > interval:
                self.count -= 1
                self.times.popleft()

        def add(self, t: datetime.datetime):
            self.times.append(t)
            self.count += 1
            self.totalCount += 1

        def act(self, t: datetime.datetime, interval):
            self.upd(t, interval=interval)
            self.add(t)

        def mark_blocked(self, t: datetime.datetime):
            self.blocked = True
            self.nextTryTime = t + self.coolDown

        def mark_unblocked(self, t: datetime.datetime):
            self.blocked = False
            self.nextTryTime = None

        def can_try(self, t: datetime.datetime = None):
            if not self.nextTryTime or (t or datetime.datetime.now()) > self.nextTryTime:
                # not set, or set and time has past nextTryTime
                return True
            return False

        def info(self) -> str:
            lastTime = self.times[-1].strftime('%m-%d %H:%M:%S') if self.times else '/'
            return f'count={self.count}, totalCnt={self.totalCount}, lastTime={lastTime}, blocked={self.blocked}'

    states: Dict[str, WorkerState] = field(init=False, default_factory=dict)
    currentResults: Dict[str, str] = field(init=False, default_factory=dict)
    logger = logging.getLogger('QueryJob')

    def __post_init__(self):
        self.states = {worker.user: QueryJob.WorkerState()
                       for worker in self.workers}

    async def init(self):
        pass
        # await self.client.send_message(some_subscriber, f'job started')
        # self.logger.info(f'job started with states: \n {self.states}')

    async def run(self, i):
        """ can do early stop if all workers are in a blocked state """
        if self.stopWhenAllBlocked and all(state.blocked for state in self.states.values()):
            self.logger.warning('all workers marked blocked, signals stop to controller')
            return True
        worker = random.choice(self.workers)
        state = self.states[worker.user]
        self.logger.info(f'pick {worker.user} for {i}-th query. state = {state.info()}')

        # so if still blocked and not yet cooled down, just skip this run.
        # This effectively slows the total frequency
        t = datetime.datetime.now()
        if state.can_try(t):
            state.mark_unblocked(t)
            state.act(t, self.stateInterval)
        else:
            self.logger.info(f'user {worker.user} still blocked, skip')
            return False
        try:
            worker.setup()
            data = worker.query()
            blocked: bool = not bool(data)
            self.logger.info(f'current earliest slots: {data}')

            goodResults = [(city, date) for city, date in data.items() if self.scorer(city, date) > 0]
            if goodResults:
                await self.on_good_results(goodResults)

            if not blocked:
                self.currentResults = data
            else:
                self.logger.error(f'user {worker.user} blocked.')
                await self.on_blocked(worker)
                state.mark_blocked(datetime.datetime.now())

        except WebDriverException as e:
            if 'disconnected: unable to connect to renderer' in e.msg \
                    or 'unknown error: net::ERR_CONNECTION_REFUSED' in e.msg:
                # unrecoverable...
                raise e
            else:
                # can recover, capture this and go on
                self.logger.warning(f'{traceback.format_exc()}')
                self.logger.warning(f'exception {e.msg}, taking snapshot')
                worker.snapshot(self.snapshotFolder, prefix='Exception')
        finally:
            pass
        return False

    async def on_blocked(self, worker: UnpaidQuery):
        # send to control channel which user blocked
        # await self.client.send_message(some_id, f'user {worker.user} blocked')
        pass

    async def on_good_results(self, goodResults):
        self.logger.warning(f'has good results: {goodResults}')
        for city, date in goodResults:
            prevDate = self.currentResults.get(city, '/')
            msg = f'{city} H: {format_h_date(prevDate)} -> {format_h_date(date)}'
            # await on sending message
            # await self.client.send_message(some_id, f'user {worker.user} blocked')


if __name__ == '__main__':
    # to be run from jupyter
    class Paths:
        log = 'logs/tele3.log'
        screenshot = '<path to screenshot>'
        teleClient = "<path to client credential ini"
        teleSession = 'sessions/sender.session'

    def get_client(session, iniFile) -> TelegramClient:
        config = configparser.ConfigParser()
        config.read(iniFile)
        client = TelegramClient(session, config['Telegram']['api_id'], config['Telegram']['api_hash'])
        return client

    driverPath = DRIVER_PATH

    runConfig = {
        'workers': [
            {
                'user': 'user1@gmail.com',
                'pswd': 'some_pswd',
                # 'proxy': '...:',
            },
            {
                'user': 'user2@gmail.com',
                'pswd': 'some_pswd',
            },
        ],
        'userAgent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
        'windowSize': [1200, 1100],
        'maxRun': 300,
        'sleep': {
            'min': 65,
            'max': 120,
        }
    }

    logFormat = '%(levelname)7s %(asctime)s %(name)-15s %(message)s'
    logging.basicConfig(filename=Paths.log, format=logFormat, level='INFO')

    # to duplicate the output also to console for better monitor
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logging.Formatter(logFormat))
    logging.getLogger().addHandler(consoleHandler)

    client = get_client(Paths.teleSession, Paths.teleClient)


    def get_workers_from_config(config: dict, driverPath: str, headless=True):
        workers = []
        for cfg in config['workers']:
            driver = get_driver(driverPath, headless=headless, proxy=cfg.get('proxy', None),
                                userAgent=config['userAgent'])
            driver.set_window_size(*config['windowSize'])
            worker = UnpaidQuery(cfg['user'], cfg['pswd'], driver)
            workers.append(worker)
        return workers


    workers = get_workers_from_config(runConfig, driverPath)

    controller = Controller(client=client,
                            sleepParam=SleepParam(min=runConfig['sleep']['min'], max=runConfig['sleep']['max']))


    def score(city, date):
        """ controls when to send message: send when > 0 """
        if '2023-02-21' <= date <= '2023-04-01':
            return 1
        else:
            return -1


    job = QueryJob(workers=workers, client=client, scorer=score, snapshotFolder=Paths.screenshot)

    controller.running = True
    await controller.run_job(job, maxRun=runConfig['maxRun'])

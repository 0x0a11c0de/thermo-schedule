import bisect
import datetime
import logging
import os
from time import sleep
from typing import Optional, Tuple
from urllib.parse import urljoin

import holidays
import requests
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import influxdb_client


SOLAR_PROD_THRESH = {
    'heat': 1.0,
    'cool': 4.0,
}

SOLAR_PROD_QUERY = '''from(bucket: "solar")
      |> range(start: -2h)
      |> filter(fn: (r) => r["_measurement"] == "realtime_energy")
      |> filter(fn: (r) => r["_field"] == "production")
      |> timedMovingAverage(every: %ss, period: 10m)
      |> last()'''


class Temperature:
    def __init__(self, hhmm, temp):
        self._hhmm = datetime.time(hhmm // 100, hhmm % 100)
        self._temp = temp

    @property
    def time(self):
        return self._hhmm

    @property
    def temperature(self):
        return self._temp


_days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def load_schedule(path: str) -> Tuple[list, dict]:
    with open(path, 'r') as inf:
        schedule = yaml.safe_load(inf)

    ret = []
    for thermostat in schedule['thermostats']:
        arr = []
        for name, sched in thermostat['schedules'].items():
            start = datetime.datetime.strptime(sched['start'], "%m/%d").date()
            sched['start'] = (start.month, start.day)
            for typ in ('heat', 'cool'):
                typ_sched = sched.get(typ)
                if typ_sched:
                    new = {}
                    holiday = None
                    peak = None
                    for day, times in typ_sched.items():
                        if day == 'peak':
                            peak = times
                            continue
                        day = day.lower()
                        temps = [Temperature(hhmm, times[hhmm]) for hhmm in sorted(times.keys())]
                        if day == 'holiday':
                            holiday = temps
                        else:
                            new[_days.index(day)] = temps
                    sched[typ]['days'] = [new[key] for key in sorted(new.keys())]
                    if len(sched[typ]['days']) != 7:
                        raise Exception("schedule does not contain all days of the week")
                    sched[typ]['holiday'] = holiday
                    sched[typ]['peak'] = peak
            arr.append(sched)
        ret.append({'url': thermostat.get('url'), 'schedule': sorted(arr, key=lambda xx: xx['start'])})
    return ret, schedule.get('settings', {})


def _is_holiday(us_holidays: holidays.HolidayBase, settings: dict, dt: datetime.datetime):
    weekday = dt.weekday()
    if weekday in (5, 6):
        return False
    holiday = us_holidays.get(dt.date())
    if not holiday:
        return False
    holiday = holiday.lower()
    for check in settings.get('holidays', []):
        if holiday.startswith(check.lower()):
            return True
    return False


def _is_peak(us_holidays: holidays.HolidayBase, settings: dict, peak: list,  dt: datetime.datetime):
    weekday = dt.weekday()
    if weekday in (5, 6):
        return False
    comp = dt.time().hour * 100 + dt.time().minute
    for row in peak:
        if row['start'] <= comp < row['end']:
            return not _is_holiday(us_holidays, settings, dt)
    return False

def _get_active_schedule(us_holidays: holidays.HolidayBase, settings: dict, schedule: [dict],
                         dt: datetime.datetime, mode: str) -> Optional[dict]:
    if not schedule:
        return None

    mmdd = (dt.month, dt.day)
    sch_idx = bisect.bisect_right(schedule, mmdd, key=lambda xx: xx['start'])
    sch_idx -= 1
    if sch_idx == 0 and mmdd < schedule[0]['start']:
        sch_idx -= 1

    peak = schedule[sch_idx]['peak']
    sched = schedule[sch_idx].get(mode)
    if not sched:
        return None

    weekday = dt.weekday()
    time = dt.time()

    is_holiday = _is_holiday(us_holidays, settings, dt)
    if is_holiday:
        day = sched['holiday']
    else:
        day = sched['days'][weekday]

    # Handle the case where we are before the first time of a new weekday and/or schedule
    if time < day[0].time:
        weekday -= 1
        if mmdd == schedule[sch_idx]['start']:
            sch_idx -= 1
            peak = schedule[sch_idx]['peak']
            sched = schedule[sch_idx].get(mode)
            if not sched:
                return None
        if _is_holiday(us_holidays, settings, dt - datetime.timedelta(days=1)):
            day = sched['holiday']
        else:
            day = sched['days'][weekday]
        return {
            'id': (sch_idx, mode, weekday, day[-1].time, day[-1].temperature),
            'temp': day[-1].temperature,
            'is_holiday': is_holiday,
            'is_peak': _is_peak(us_holidays, settings, peak, dt),
            'peak_temp': sched['peak'],
        }

    ii = bisect.bisect_right(day, time, key=lambda xx: xx.time) - 1
    return {
        'id': (sch_idx, mode, weekday, day[ii].time, day[ii].temperature),
        'temp': day[ii].temperature,
        'is_holiday': is_holiday,
        'is_peak': _is_peak(us_holidays, settings, peak, dt),
        'peak_temp': sched['peak'],
    }


def _equiv_temps(left, right):
    return round(left * 10) == round(right * 10)


def _set_temp(log: logging.Logger, uri: str, data: dict, timeout: float):
    tries = 3
    while tries > 0:
        try:
            resp = requests.post(urljoin(uri, '/control'), data=data, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            if tries == 1:
                raise Exception(f'failed to set new state: {exc}')
            log.error(f'failure in _set_temp, will retry: %s', exc)
            sleep(3)
            tries -= 1
            continue

        sleep(3)

        try:
            resp = requests.get(urljoin(uri, '/query/info'), timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            if tries == 1:
                raise Exception(f'failed to set new state: {exc}')
            log.error(f'failure in _set_temp, will retry: %s', exc)
            tries -= 1
            continue

        resp = resp.json()
        mode = data['mode']
        if mode == 1:
            key = 'heattemp'
        elif mode == 2:
            key = 'cooltemp'
        else:
            raise Exception(f'invalid mode: {mode}')
        if _equiv_temps(resp[key], data[key]):
            return

        tries -= 1
    raise Exception('failed to set new state')


class Data:
    def __init__(self, log, thermo, settings):
        self.log: logging.Logger = log
        self.schedule: dict = thermo['schedule']
        self.settings: dict = settings
        self.uri: str = thermo['url']
        self.interval_secs: float = settings.get('interval', 60)
        self.timeout: float = settings.get('timeout', 3)
        self.state: tuple = ()


def get_solar_prod(data: Data) -> Optional[float]:
    solar_prod = None
    try:
        influxdb = data.settings['influxdb']
        interval = data.settings['interval'] // 2
        query = SOLAR_PROD_QUERY % interval
        result = influxdb['query_api'].query(org=influxdb['org'], query=query)
        for table in result:
            for record in table.records:
                solar_prod = record.get_value()
                break
    except Exception:
        data.log.exception('failed to get solar production')
    return solar_prod


def thermo_task(data: Data):
    if data.log.isEnabledFor(logging.DEBUG):
        data.log.debug('starting thermo task')

    us_holidays = holidays.UnitedStates(observed=True)

    # noinspection PyBroadException
    try:
        resp = requests.get(urljoin(data.uri, '/query/info'), timeout=data.timeout)
        resp.raise_for_status()

        resp = resp.json()
        data.log.debug('%s', resp)

        mode = resp['mode']
        # Don't do anything if the schedule is on or the mode is off or auto
        if resp['schedule'] or mode in (0, 3):
            return

        if mode == 1:
            mode_str = 'heat'
        elif mode == 2:
            mode_str = 'cool'
        else:
            raise Exception(f'invalid mode: {mode}')

        now = datetime.datetime.now()
        new_sched = _get_active_schedule(us_holidays, data.settings, data.schedule, now, mode_str)
        if new_sched is None:
            data.log.debug('no schedule set')
            return

        solar_prod_thresh = SOLAR_PROD_THRESH[mode_str]

        solar_prod = get_solar_prod(data)
        if solar_prod is None:
            data.log.warning('solar production not available, setting to 0')
            solar_prod = 0

        is_holiday = new_sched['is_holiday']
        is_peak = new_sched['is_peak']
        peak_temp = new_sched['peak_temp']

        if is_peak and solar_prod < solar_prod_thresh:
            data.log.info(f'solar production is below threshold, changing to peak temp: {peak_temp}')
            new_sched['temp'] = peak_temp

        new_sched["id"] = new_sched["id"] + (new_sched["temp"],)

        if mode_str == 'heat':
            check = 'heattemp'
            heattemp = new_sched['temp']
            cooltemp = resp['cooltemp']
        elif mode_str == 'cool':
            check = 'cooltemp'
            heattemp = resp['heattemp']
            cooltemp = new_sched['temp']
        else:
            raise Exception('invalid mode_str: ' + mode_str)

        data.log.debug(f'mode={mode_str} check={check} heattemp={heattemp} '
                       f'cooltemp={cooltemp} state={data.state} new_state={new_sched["id"]} '
                       f'is_holiday={is_holiday} '
                       f'is_peak={is_peak} '
                       f'solar_prod={solar_prod}')

        if data.state != new_sched['id']:
            api_data = dict(mode=mode, heattemp=heattemp, cooltemp=cooltemp)
            data.log.info(
                f'updating thermostat: mode={mode_str} '
                f'heattemp={heattemp} '
                f'cooltemp={cooltemp} '
                f'is_holiday={is_holiday} '
                f'is_peak={is_peak} '
                f'solar_prod={solar_prod}')
            _set_temp(data.log, data.uri, api_data, data.timeout)
            data.state = new_sched['id']
        else:
            data.log.debug('already in the desired state')
    except Exception:
        data.log.exception('unknown failure')


def main():
    log = logging.getLogger('thermo')
    log.setLevel(getattr(logging, os.environ.get('LOGLEVEL', 'INFO')))
    thermostats, settings = load_schedule(os.environ.get('SCHEDULE', '/config/schedule.yaml'))
    interval_secs_requested = min(int(settings.get('interval', 60)), 60)
    interval_secs = 60 // int(round(60 / interval_secs_requested))

    if interval_secs_requested != interval_secs:
        log.warning(f'interval was adjusted to {interval_secs}')

    if interval_secs < 60:
        cron = CronTrigger(second=f'*/{interval_secs}')
    else:
        cron = CronTrigger(minute='*')

    trigger = OrTrigger([DateTrigger(), cron])

    scheduler = BlockingScheduler({
        'apscheduler.job_defaults.coalesce': 'true',
        'apscheduler.job_defaults.max_instances': '1',
    })

    for thermo in thermostats:
        data = Data(log, thermo, settings)
        scheduler.add_job(lambda: thermo_task(data), trigger)

    influxdb = settings['influxdb']
    client = influxdb_client.InfluxDBClient(url=influxdb['url'], token=influxdb['token'], org=influxdb['org'], verify_ssl=False)
    influxdb['query_api'] = client.query_api()

    scheduler.start()


if __name__ == "__main__":
    logging.basicConfig(format='%(levelname)s:%(asctime)s:%(message)s', level=logging.WARNING)
    main()

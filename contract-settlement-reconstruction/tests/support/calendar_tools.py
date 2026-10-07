import calendar
from datetime import date,timedelta

def parse(value):return date.fromisoformat(value)

def add_months(start,months,anchor):
    serial=start.year*12+(start.month-1)+months
    year,month=divmod(serial,12);month+=1
    day=min(anchor,calendar.monthrange(year,month)[1])
    return date(year,month,day)

def modified_following(day,holidays):
    def business(x):return x.weekday()<5 and x.isoformat() not in holidays
    if business(day):return day
    forward=day
    while not business(forward):forward+=timedelta(days=1)
    if forward.month==day.month:return forward
    backward=day
    while not business(backward):backward-=timedelta(days=1)
    return backward

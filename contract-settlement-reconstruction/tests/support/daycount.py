from decimal import Decimal

def fraction(start,end,convention):
    if convention=="ACT/360":return Decimal((end-start).days)/Decimal(360)
    if convention=="ACT/365F":return Decimal((end-start).days)/Decimal(365)
    if convention=="30E/360":
        d1=min(start.day,30);d2=min(end.day,30)
        days=360*(end.year-start.year)+30*(end.month-start.month)+(d2-d1)
        return Decimal(days)/Decimal(360)
    raise ValueError("unknown convention")

"""Application factice du bac à sable gitflow."""


def saluer(name):
    return f"Bonjour {name.strip()}"


def total(prices):
    return sum(p for p in prices or [])


def discount(price, rate):
    rate = max(rate, 0)
    return round(price * (1 - rate), 2)


def feature_a():
    return 'A'


def feature_b():
    return 'B'


greet = saluer

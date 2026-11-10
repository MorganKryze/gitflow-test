"""Application factice du bac à sable gitflow."""


def greet(name):
    return f"Bonjour {name}"


def total(prices):
    return sum(prices or [])


def discount(price, rate):
    return round(price * (1 - rate), 2)


def feature_a():
    return 'A'


def feature_c():
    return 'C'

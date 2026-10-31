from pkg.models import Logger, User


def process() -> None:
    u = User()
    u.save()  # -> User.save, NOT Logger.save

    log = Logger()
    log.save()  # -> Logger.save, NOT User.save

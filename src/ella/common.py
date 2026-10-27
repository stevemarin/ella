
from typing import Protocol, Self


class Singleton(Protocol):
    __Singleton_instance: Self

    def __new__(cls):
        try:
            return cls.__Singleton_instance
        except AttributeError:
            instance = super().__new__(cls)
            cls.__Singleton_instance = instance
            return instance

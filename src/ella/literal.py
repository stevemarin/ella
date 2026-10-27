from abc import ABC

from ella.token import Token


class Literal(Token, ABC):
    __slots__ = ()


# fmt: off
class Integer(Literal):...
class Float(Literal): ...
class String(Literal): ...
class Comment(Literal): ...
# fmt: on

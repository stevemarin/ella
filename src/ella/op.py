from abc import ABC
from enum import IntEnum
from typing import ClassVar


class Precedence(IntEnum):
    Assignment = 10
    Lambda = 20
    Conditional = 30
    LogicalOr = 40
    LogicalAnd = 50
    LogicalNot = 60
    Comparison = 70  # also in and is
    BitwiseOr = 80
    BitwiseXor = 90
    BitwiseAnd = 100
    Shift = 110
    Factor = 120
    Product = 130  # also %
    Prefix = 140
    Exponent = 150
    Postfix = 160
    Group = 170
    Call = 180


class Associativity(IntEnum):
    LEFT = -1
    RIGHT = 1


class Op(ABC):
    string: str
    associativity: Associativity
    precedence: int


class Unary(Op, ABC): ...


class Prefix(Unary, ABC):
    _types: ClassVar[dict[str, type[Prefix]]] = {}

    def __init_subclass__(cls, string: str, precedence: int) -> None:
        cls.string = string
        cls.precedence = precedence

        Prefix._types[string] = cls
        return super().__init_subclass__()


# fmt: off
class Not(Prefix, string="not", precedence=Precedence.LogicalNot): ...
class Negative(Prefix, string="-", precedence=Precedence.Prefix): ...
class Positive(Prefix, string="+", precedence=Precedence.Prefix): ...
# fmt: on


class Postfix(Unary, ABC):
    _types: ClassVar[dict[str, type[Postfix]]] = {}

    def __init_subclass__(cls, string: str, precedence: int) -> None:
        cls.string = string
        cls.precedence = precedence

        Postfix._types[string] = cls
        return super().__init_subclass__()


class Call(Postfix, string="(", precedence=Precedence.Call): ...


class Binary(Op, ABC):
    _types: ClassVar[dict[str, type[Binary]]] = {}

    def __init_subclass__(
        cls,
        string: str,
        precedence: int,
        associativity: Associativity = Associativity.LEFT,
    ) -> None:
        cls.string = string
        cls.precedence = precedence
        cls.associativity = associativity

        Binary._types[string] = cls
        return super().__init_subclass__()


# fmt: off
class Assignment(Binary, string="=", precedence=Precedence.Assignment, associativity=Associativity.RIGHT): ...
class Conditional(Binary, string="if", precedence=Precedence.Conditional): ...
class LogicalOr(Binary, string="or", precedence=Precedence.LogicalOr): ...
class LogicalAnd(Binary, string="and", precedence=Precedence.LogicalAnd): ...
class Greater(Binary, string=">", precedence=Precedence.Comparison): ...
class GreaterOrEqual(Binary, string=">=", precedence=Precedence.Comparison): ...
class Equal(Binary, string="==", precedence=Precedence.Comparison): ...
class LessOrEqual(Binary, string="<=", precedence=Precedence.Comparison): ...
class Less(Binary, string="<", precedence=Precedence.Comparison): ...
class BitwiseOr(Binary, string="|", precedence=Precedence.BitwiseOr): ...
class BitwiseXor(Binary, string="^", precedence=Precedence.BitwiseXor): ...
class BitwiseAnd(Binary, string="&", precedence=Precedence.BitwiseAnd): ...
class LeftShift(Binary, string="<<", precedence=Precedence.Shift): ...
class RightShift(Binary, string=">>", precedence=Precedence.Shift): ...
class Add(Binary, string="+", precedence=Precedence.Factor): ...
class Subtract(Binary, string="-", precedence=Precedence.Factor): ...
class Multiply(Binary, string="*", precedence=Precedence.Product): ...
class Divide(Binary, string="/", precedence=Precedence.Product): ...
class FloorDivide(Binary, string="//", precedence=Precedence.Product): ...
class Exponent(Binary, string="**", precedence=Precedence.Exponent, associativity=Associativity.RIGHT): ...
# fmt: on

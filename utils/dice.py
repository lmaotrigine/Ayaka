from __future__ import annotations

import re

import dice_parser


ADV_WORD_RE = re.compile(r'(?:^|\s+)(adv|dis)(?:\s+|$)')


def string_search_adv(dice_str: str) -> tuple[str, dice_parser.AdvType]:
    adv = dice_parser.AdvType.NONE
    if (match := ADV_WORD_RE.search(dice_str)) is not None:
        adv = dice_parser.AdvType.ADV if match.group(1) == 'adv' else dice_parser.AdvType.DIS
        return dice_str[: match.start(1)] + dice_str[match.end() :], adv
    return dice_str, adv


class VerboseMDStringifier(dice_parser.MarkdownStringifier):
    def str_expression(self, node: dice_parser.Expression) -> str:
        return f'**{node.comment or "Result"}**: {self.stringify_node(node.roll)}\n**Total**: {int(node.total)}'


class PersistentRollContext(dice_parser.RollContext):
    def __init__(self, max_rolls: int = 1000, max_total_rolls: int | None = None) -> None:
        super().__init__(max_rolls)
        self.max_total_rolls = max_total_rolls or max_rolls
        self.total_rolls = 0

    def count_roll(self, n: int = 1) -> None:
        super().count_roll(n)
        self.total_rolls += 1
        if self.total_rolls > self.max_total_rolls:
            raise dice_parser.TooManyRolls('Too many dice rolled.')


class RerollableStringifier(dice_parser.SimpleStringifier):
    def stringify_node(self, node: dice_parser.Number) -> str | None:
        if not node.kept:
            return None
        return super().stringify_node(node)

    def str_expression(self, node: dice_parser.Expression) -> str | None:
        return self.stringify_node(node.roll)

    def str_literal(self, node: dice_parser.Literal) -> str:
        return str(node.total)

    def str_parenthetical(self, node: dice_parser.Parenthetical) -> str:
        return f'({self.stringify_node(node.value)})'

    def str_set(self, node: dice_parser.Set) -> str:
        out = ', '.join([self.stringify_node(v) for v in node.values if v.kept])  # type: ignore
        if len(node.values) == 1:
            return f'({out},)'
        return f'({out})'

    def str_dice(self, node: dice_parser.Dice) -> str:
        return self.str_set(node)

    def str_die(self, node: dice_parser.Die) -> str:
        return str(node.total)


def d20_with_adv(adv: dice_parser.AdvType | int) -> str:
    if adv is dice_parser.AdvType.NONE:
        return '1d20'
    elif adv is dice_parser.AdvType.ADV:
        return '2d20kh1'
    elif adv is dice_parser.AdvType.DIS:
        return '2d20kl1'
    elif adv == 2:
        return '3d20kh1'
    return '1d20'


def get_roll_comment(expr: str) -> tuple[str, str]:
    result = dice_parser.parse(expr, allow_comments=True)
    return str(result.roll), (result.comment or '')

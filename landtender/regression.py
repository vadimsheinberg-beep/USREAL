"""Метод наименьших квадратов на чистой стандартной библиотеке.

Проекту хватает одной зависимости — ``requests``. Тянуть numpy со scikit-learn
ради подгонки прямой по десятку точек несоразмерно, а нормальные уравнения
решаются в тридцати строках.

Модель намеренно линейная и объяснимая: по каждому коэффициенту видно, что
именно он утверждает о цене. Градиентный бустинг на выборке из двенадцати
сделок дал бы красивее число и хуже смысл.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Fit:
    """Результат подгонки, вместе с признанием собственной точности."""

    #: Свободный член и коэффициенты — по одному на признак, в том же порядке.
    coefficients: list[float]
    #: Доля объяснённой дисперсии. Отрицательная означает, что модель хуже
    #: простого среднего, — такую подгонку показывать нельзя.
    r_squared: float
    #: Стандартное отклонение остатков: на столько типично ошибается модель.
    residual_std: float
    #: Сколько наблюдений участвовало.
    n: int
    #: Сколько признаков (без свободного члена).
    k: int

    @property
    def usable(self) -> bool:
        """Стоит ли вообще показывать такой прогноз.

        Наблюдений должно быть заметно больше, чем коэффициентов: подгонка по
        трём точкам с тремя признаками пройдёт идеально и не будет значить
        ничего. И модель обязана быть лучше среднего.
        """
        return self.n >= self.k + 5 and self.r_squared > 0.0

    def predict(self, features: list[float]) -> float | None:
        if len(features) != self.k:
            return None
        value = self.coefficients[0]
        for coefficient, feature in zip(self.coefficients[1:], features):
            value += coefficient * feature
        return value


def fit(rows: list[list[float]], targets: list[float]) -> Fit | None:
    """Подгоняет ``target = b0 + b1·x1 + …`` по методу наименьших квадратов.

    Возвращает ``None``, когда решать нечего: пустая выборка, несовпадающие
    длины или вырожденная система (например, признак с одинаковым значением
    во всех строках).
    """
    n = len(rows)
    if n == 0 or n != len(targets):
        return None
    k = len(rows[0])
    if k == 0 or any(len(row) != k for row in rows):
        return None
    if n < k + 2:
        return None

    # Матрица плана со свободным членом первым столбцом.
    design = [[1.0, *row] for row in rows]
    width = k + 1

    # Нормальные уравнения XᵀX·b = Xᵀy.
    xtx = [[sum(design[i][a] * design[i][b] for i in range(n)) for b in range(width)]
           for a in range(width)]
    xty = [sum(design[i][a] * targets[i] for i in range(n)) for a in range(width)]

    coefficients = _solve(xtx, xty)
    if coefficients is None:
        return None

    mean = sum(targets) / n
    ss_total = sum((y - mean) ** 2 for y in targets)
    residuals = [
        targets[i] - sum(coefficients[a] * design[i][a] for a in range(width))
        for i in range(n)
    ]
    ss_residual = sum(r * r for r in residuals)

    r_squared = 1.0 - ss_residual / ss_total if ss_total > 0 else 0.0
    # Несмещённая оценка: делим на число степеней свободы, а не на n.
    degrees = max(n - width, 1)
    residual_std = math.sqrt(ss_residual / degrees)

    return Fit(
        coefficients=coefficients,
        r_squared=r_squared,
        residual_std=residual_std,
        n=n,
        k=k,
    )


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Гаусс с выбором ведущего элемента. ``None`` — система вырождена."""
    size = len(vector)
    rows = [list(matrix[i]) + [vector[i]] for i in range(size)]

    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) < 1e-12:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]

        head = rows[column][column]
        for other in range(size):
            if other == column:
                continue
            factor = rows[other][column] / head
            if factor:
                for c in range(column, size + 1):
                    rows[other][c] -= factor * rows[column][c]

    return [rows[i][size] / rows[i][i] for i in range(size)]


def median(values: list[float]) -> float | None:
    """Медиана — запасной вариант, когда регрессию строить не на чем."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2

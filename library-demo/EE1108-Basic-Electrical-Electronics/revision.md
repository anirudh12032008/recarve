# Revision — Basic Electrical & Electronics Engineering

## Covered

**Lecture 1 (2026-09-10):** Kirchhoff's Current and Voltage Laws, each derived
from a conservation principle before being stated; sign conventions for loop
traversal; a two-loop circuit solved for three branch currents.

---

## Formulas

| Formula | Meaning | Lecture |
|---------|---------|---------|
| $\sum I_{\text{in}} = \sum I_{\text{out}}$ | KCL — conservation of charge at a node | 1 |
| $\sum V = 0$ | KVL — conservation of energy around a loop | 1 |
| $V = IR$ | Ohm's law, used throughout | 1 |
| $\frac{1}{R} = \frac{1}{R_1} + \frac{1}{R_2}$ | Resistors in parallel | 1 |

---

## Threads

**Neither law is a new assumption.** Both were derived, not asserted — KCL from
conservation of charge, KVL from conservation of energy. A question asking you
to "state and explain" wants the conservation principle, not just the equation.

**Sign convention is the examinable skill.** The physics is two lines; the marks
are in setting up consistent loop directions and reading a negative answer
correctly. Practise circuits where at least one assumed direction is wrong, so
the negative result stops being alarming.

---

## Likely questions

**1. State both of Kirchhoff's laws and the conservation principle behind each.**

<details><summary>Answer</summary>

**KCL:** the algebraic sum of currents at a node is zero. From conservation of
charge — charge cannot accumulate at a junction.

**KVL:** the algebraic sum of potential differences around a closed loop is
zero. From conservation of energy — returning to the starting point means
returning to the same potential.

</details>

**2. Two resistors, 3 Ω and 6 Ω, are in parallel across 12 V. Find the total current and the current in each branch.**

<details><summary>Answer</summary>

$\frac{1}{R} = \frac{1}{3} + \frac{1}{6} = \frac{1}{2}$, so $R = 2\,\Omega$ and
$I = \frac{12}{2} = 6$ A.

Each branch sees the full 12 V: $I_{3\Omega} = 4$ A and $I_{6\Omega} = 2$ A,
which sum to 6 A as KCL requires.

</details>

**3. What does a negative current in your final answer tell you?**

<details><summary>Answer</summary>

That the magnitude is correct but the true direction is opposite to the one
assumed at the start. Since the assumed direction is arbitrary, this is the
method reporting the correct physical answer, not an error to be fixed.

</details>

---

## Gaps

Thevenin and Norton equivalents were named as coming later and are not covered.
AC circuits have not been introduced — everything here is DC.

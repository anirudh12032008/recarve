# Revision — Mathematics 1

## Covered

**Lecture 1 (2026-09-08):** The definite integral as the limit of a Riemann
sum; both parts of the Fundamental Theorem of Calculus; $\int_0^1 x^2\,dx$
computed twice, once from the definition and once from the theorem.

**Lecture 2 (2026-09-11):** Integration by substitution as the chain rule
reversed; choosing $u$; adjusting by a constant; changing the limits on a
definite integral instead of substituting back.

---

## Formulas

| Formula | Meaning | Lecture |
|---------|---------|---------|
| $\int_a^b f(x)\,dx = \lim_{n\to\infty}\sum_{i=1}^{n} f(x_i)\Delta x$ | The definite integral as a Riemann sum | 1 |
| $F(x) = \int_a^x f(t)\,dt \Rightarrow F'(x) = f(x)$ | Fundamental Theorem, Part 1 | 1 |
| $\int_a^b f(x)\,dx = F(b) - F(a)$ | Fundamental Theorem, Part 2 | 1 |
| $\int f(g(x))g'(x)\,dx = \int f(u)\,du$ | Substitution | 2 |

---

## Threads

**The theorem is the whole point.** Lecture 1 spent most of its time on a
definition nobody will use for computation. That was deliberate: the Riemann
sum is what an integral *is*, and the Fundamental Theorem is the claim — far
from obvious — that this geometric quantity can be got at by reversing
differentiation. Every technique after this, substitution included, is a way of
finding antiderivatives.

**Substitution is the chain rule with the arrow turned round.** Lecture 2 never
introduced a new idea, only a new direction. This is worth holding onto,
because the techniques still coming — parts, partial fractions — are each some
differentiation rule run backwards in the same way.

---

## Likely questions

**1. State the Fundamental Theorem of Calculus and explain why it matters.**

<details><summary>Answer</summary>

**Part 1:** If $F(x) = \int_a^x f(t)\,dt$ for continuous $f$, then $F'(x) = f(x)$.

**Part 2:** If $F$ is any antiderivative of $f$, then $\int_a^b f(x)\,dx = F(b) - F(a)$.

It matters because it replaces an infinite limiting process — the Riemann sum —
with a finite computation: find an antiderivative, evaluate it at two points,
subtract.

</details>

**2. Evaluate $\int_1^3 (2x+1)\,dx$.**

<details><summary>Answer</summary>

$F(x) = x^2 + x$, so $F(3) - F(1) = 12 - 2 = 10$.

</details>

**3. Evaluate $\int x e^{x^2}\,dx$.**

<details><summary>Answer</summary>

Let $u = x^2$, $du = 2x\,dx$, so $x\,dx = \frac{1}{2}du$:
$$\frac{1}{2}\int e^u\,du = \frac{1}{2}e^{x^2} + C$$

</details>

**4. Evaluate $\int_0^2 x(x^2+1)^3\,dx$.**

<details><summary>Answer</summary>

With $u = x^2+1$ the limits become 1 and 5:
$$\frac{1}{2}\int_1^5 u^3\,du = \frac{1}{8}(625-1) = 78$$

</details>

---

## Gaps

Integration by parts has been named but not taught. Partial fractions has not
been mentioned. Neither appears in these two lectures, so neither is revised
here.

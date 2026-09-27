"""Замер скорости эмбеддера: сколько кусков кода в секунду он переваривает.

    docker compose exec -T kb python -m kb.embed_speed
    docker compose exec -T kb python -m kb.embed_speed --chunks 950000

Шлёт в эмбеддер (тот же, что у индексации, из .env) пачки по 32 куска
кода средней длины — как kb.code_index — и печатает скорость. С --chunks
пересчитывает её во время индексации. Ничего не пишет ни в Qdrant, ни на
диск.

Во время замера эмбеддер занят: не запускать одновременно с индексацией,
иначе обе цифры будут занижены.
"""

from __future__ import annotations

import argparse
import sys
import time

from kb import config
from kb.embedder import EmbedError, embed_batch

# Кусок кода ~1.5 КБ: столько в среднем уходит в вектор (шапка + функция).
# Номер в тексте — чтобы пачки отличались и ничего не взялось из кеша
SAMPLE = '''billing/src/payments/processor.py :: PaymentProcessor.charge_{n}
def charge_{n}(self, order, card, retries=3):
    """Списывает сумму заказа {n} с карты, повторяет при сетевых ошибках."""
    amount = order.total_with_discounts()
    if amount <= 0:
        raise ValueError("сумма заказа должна быть положительной")
    for attempt in range(retries):
        try:
            response = self.gateway.charge(card.token, amount, currency=order.currency)
            if response.status == "declined":
                self.audit.log(order.id, "declined", response.reason)
                return PaymentResult(ok=False, reason=response.reason)
            self.ledger.record(order.id, amount, response.transaction_id)
            self.notifier.send(order.customer, "payment_ok", amount=amount)
            return PaymentResult(ok=True, transaction_id=response.transaction_id)
        except GatewayTimeout:
            self.metrics.increment("gateway_timeout")
            time.sleep(2 ** attempt)
    self.audit.log(order.id, "failed", "retries exhausted")
    return PaymentResult(ok=False, reason="gateway unavailable")
'''


def num(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def hours(seconds: float) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер скорости эмбеддера")
    ap.add_argument("--batches", type=int, default=10, help="сколько пачек отправить (по умолчанию 10)")
    ap.add_argument("--batch", type=int, default=32, help="размер пачки, как у kb.code_index (32)")
    ap.add_argument("--chunks", type=int, default=0, help="сколько чанков предстоит — пересчитать во время")
    args = ap.parse_args()

    print(f"Эмбеддер: {config.embeddings_url()}, модель {config.EMBED_MODEL}")
    n = 0

    def batch() -> list[str]:
        nonlocal n
        out = [SAMPLE.format(n=n + i) for i in range(args.batch)]
        n += args.batch
        return out

    # Первая пачка — прогрев: модель может загружаться в память, это не скорость
    try:
        t = time.monotonic()
        embed_batch(batch())
        print(f"прогрев: {time.monotonic() - t:.1f} с")
    except EmbedError as e:
        print(f"Эмбеддер не ответил: {e}")
        return 1

    times = []
    for i in range(args.batches):
        t = time.monotonic()
        try:
            embed_batch(batch())
        except EmbedError as e:
            print(f"пачка {i + 1}: ошибка — {e}")
            return 1
        times.append(time.monotonic() - t)
        print(f"  пачка {i + 1}/{args.batches}: {times[-1]:.1f} с", end="\r")

    total = sum(times)
    speed = args.batch * len(times) / total
    print(f"\nСкорость: {speed:.1f} чанков в секунду, {num(speed * 3600)} в час")
    print(f"пачка из {args.batch}: в среднем {total / len(times):.1f} с, "
          f"самая долгая {max(times):.1f} с")
    if args.chunks:
        print(f"{num(args.chunks)} чанков -> около {hours(args.chunks / speed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

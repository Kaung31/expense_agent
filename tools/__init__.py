"""Deterministic tools the agents call.

Side-effectful or fraud-relevant work lives here as *plain, testable functions* —
never at the model's discretion (guide §8: side-effects sit behind deterministic
gates). Each tool has an offline implementation so the whole pipeline runs in CI
with zero cloud dependencies.
"""

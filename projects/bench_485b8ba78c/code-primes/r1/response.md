ファイル: main.py
```python
def is_prime(n):
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True

for n in range(2, 101):
    if is_prime(n):
        print(n)
```
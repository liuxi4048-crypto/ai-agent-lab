We need to output code for a Python script that prints primes from 2 to 100 inclusive, each on a separate line. Use plain standard library; no arguments or input.

We should not have extra lines or text, just output exactly the code block containing main.py. We need to include the line "ファイル: main.py" before the code block? The instructions: "出力形式: 次の1行を書いてから、``python フェンスの中にファイル全文を入れてください。ファイル: main.py"

So we should first write "ファイル: main.py" and then the Python code fenced block.

We should ensure that the script prints primes: 2,3,5,7,11,13,17,19,23,29,31,37,41,43,47,53,59,61,67,71,73,79,83,89,97. That's 25 numbers.

We need to implement primes quickly. For 2 to 100 inclusive. Implementation: is_prime function or simple sieve. Simpler: check each integer n: if n>=2 and all n%k !=0 for k in range(2,int(sqrt(n))+1). Print each prime with print(n).

We need no extra whitespace lines. And script termination.

Thus answer:

ファイル: main.py

```python
#!/usr/bin/env python3
# main.py

def is_prime(n):
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True

for num in range(2, 101):
    if is_prime(num):
        print(num)
```

But the requirement: no extra characters or blank lines. Ensure there is no shebang? It didn't mention shebang. But including shebang is okay. Not harmful. We should keep only code, no comments? They didn't forbid comments. But better minimal code. But comments are okay? Probably okay. But minimal risk: no comments, just code.

Simplest:

```
def is_prime(n):
    if n < 2: return False
    if n % 2 == 0: return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0: return False
        i += 2
    return True

for n in range(2, 101):
    if is_prime(n):
        print(n)
```

We don't need to wrap in functions or main. That prints.

Ensure no trailing spaces. Ensure newlines appropriately. Single blank line at start? No. So output "ファイル: main.py" then code block.

Hence final.
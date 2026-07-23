# TODO CLI Plan

## Files
- `todo.py`: Main script
- `tasks.json`: Stores tasks (created automatically)

## Commands
- `add <task>`: Add a new task
- `list`: List all tasks with status
- `done <index>`: Mark task as done (1-based index)

## Implementation Steps
1. Use `argparse` to handle subcommands.
2. Load `tasks.json` (create if missing).
3. For `add`, append new task with `id`, `task`, `completed=False`.
4. For `list`, print each task with status.
5. For `done`, set `completed=True` for the specified index.

## Test Commands
```bash
python todo.py add "Buy milk"
python todo.py list
python todo.py done 1
```
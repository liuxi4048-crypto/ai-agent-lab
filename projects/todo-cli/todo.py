import argparse
import json
import os

tasks_file = 'tasks.json'

tasks = []
if os.path.exists(tasks_file):
    with open(tasks_file, 'r') as f:
        tasks = json.load(f)
else:
    with open(tasks_file, 'w') as f:
        json.dump([], f)

parser = argparse.ArgumentParser(description='TODO CLI')
subparsers = parser.add_subparsers(dest='command')

add_parser = subparsers.add_parser('add')
add_parser.add_argument('task', type=str, help='Task description')

list_parser = subparsers.add_parser('list')

done_parser = subparsers.add_parser('done')
done_parser.add_argument('index', type=int, help='Task index to mark as done')

args = parser.parse_args()

if args.command == 'add':
    tasks.append({
        'id': len(tasks) + 1,
        'task': args.task,
        'completed': False
    })
    with open(tasks_file, 'w') as f:
        json.dump(tasks, f, indent=2)

elif args.command == 'list':
    for task in tasks:
        status = 'X' if task['completed'] else ' '
        print(f"[{status}] {task['id']}: {task['task']}")

elif args.command == 'done':
    if 1 <= args.index <= len(tasks):
        tasks[args.index - 1]['completed'] = True
        with open(tasks_file, 'w') as f:
            json.dump(tasks, f, indent=2)
    else:
        print('Invalid index')

import tkinter as tk

def button_click(char):
    current = display_var.get()
    display_var.set(current + char)

def clear():
    display_var.set("")

def calculate():
    try:
        result = eval(display_var.get())
        display_var.set(str(result))
    except:
        display_var.set("Error")

root = tk.Tk()
root.title("Calculator")


display_var = tk.StringVar()
display = tk.Label(root, textvariable=display_var, anchor="e", width=20, height=2, bg="white")
display.grid(row=0, column=0, columnspan=4)

buttons = [
    ['7', '8', '9', '/'],
    ['4', '5', '6', '*'],
    ['1', '2', '3', '-'],
    ['0', 'C', '=', '+']
]

for row_idx, row in enumerate(buttons):
    for col_idx, char in enumerate(row):
        if char == 'C':
            cmd = clear
        elif char == '=':
            cmd = calculate
        else:
            cmd = lambda c=char: button_click(c)
        btn = tk.Button(root, text=char, command=cmd, width=5, height=2)
        btn.grid(row=row_idx+1, column=col_idx)

root.mainloop()
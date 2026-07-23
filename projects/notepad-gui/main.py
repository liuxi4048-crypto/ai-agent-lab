import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
text = tk.Text(root)
text.pack()

def save():
    content = text.get("1.0", "end")
    file_path = filedialog.asksaveasfilename()
    if file_path:
        with open(file_path, 'w') as f:
            f.write(content)

menu = tk.Menu(root)
root.config(menu=menu)
file_menu = tk.Menu(menu)
menu.add_cascade(label="File", menu=file_menu)
file_menu.add_command(label="Save", command=save)

root.mainloop()
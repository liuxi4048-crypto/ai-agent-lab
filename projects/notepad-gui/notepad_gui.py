# -*- coding: utf-8 -*-
"""
Simple Notepad GUI using tkinter.

Only standard library is used; the script should compile without errors.
"""
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

class NotepadApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Untitled - Notepad")
        self.file_path: str | None = None
        self._setup_widgets()
        self._setup_menu()
        self._setup_bindings()

    def _setup_widgets(self):
        # Text widget with scrollbars
        self.text_area = tk.Text(self.root, undo=True)
        self.scroll_y = tk.Scrollbar(self.root, command=self.text_area.yview)
        self.scroll_x = tk.Scrollbar(self.root, orient='horizontal', command=self.text_area.xview)
        self.text_area.configure(yscrollcommand=self.scroll_y.set, xscrollcommand=self.scroll_x.set)
        self.text_area.pack(fill=tk.BOTH, expand=1, side=tk.LEFT)
        self.scroll_y.pack(fill=tk.Y, side=tk.RIGHT)
        self.scroll_x.pack(fill=tk.X, side=tk.BOTTOM)

    def _setup_menu(self):
        menubar = tk.Menu(self.root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="New", accelerator="Ctrl+N", command=self._new_file)
        filemenu.add_command(label="Open...", accelerator="Ctrl+O", command=self._open_file)
        filemenu.add_separator()
        filemenu.add_command(label="Save", accelerator="Ctrl+S", command=self._save_file)
        filemenu.add_command(label="Save As...", accelerator="Ctrl+Shift+S", command=self._save_as)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", accelerator="Ctrl+Q", command=self._exit)
        menubar.add_cascade(label="File", menu=filemenu)
        self.root.config(menu=menubar)

    def _setup_bindings(self):
        self.root.bind_all("<Control-n>", lambda e: self._new_file())
        self.root.bind_all("<Control-o>", lambda e: self._open_file())
        self.root.bind_all("<Control-s>", lambda e: self._save_file())
        self.root.bind_all("<Control-Shift-S>", lambda e: self._save_as())
        self.root.bind_all("<Control-q>", lambda e: self._exit())

    def _new_file(self):
        if self._maybe_save():
            self.text_area.delete(1.0, tk.END)
            self.file_path = None
            self.root.title("Untitled - Notepad")

    def _open_file(self):
        if not self._maybe_save():
            return
        path = filedialog.askopenfilename(
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.text_area.delete(1.0, tk.END)
                self.text_area.insert(tk.END, content)
                self.file_path = path
                self.root.title(f"{os.path.basename(path)} - Notepad")
            except Exception as e:
                messagebox.showerror("Error", f"Could not open file:\n{e}")

    def _save_file(self):
        if self.file_path:
            return self._write_to_path(self.file_path)
        else:
            return self._save_as()

    def _save_as(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if path:
            success = self._write_to_path(path)
            if success:
                self.file_path = path
                self.root.title(f"{os.path.basename(path)} - Notepad")
            return success
        return False

    def _write_to_path(self, path: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as f:
                text = self.text_area.get(1.0, tk.END)
                f.write(text.rstrip('\n'))  # avoid adding extra newline at EOF
            return True
        except Exception as e:
            messagebox.showerror("Error", f"Could not save file:\n{e}")
            return False

    def _maybe_save(self) -> bool:
        """Prompt to save if the document has unsaved changes.
        Returns True if it's safe to continue (i.e., user saved or discarded).
        """
        if self.text_area.edit_modified():
            response = messagebox.askyesnocancel("Save", "Do you want to save changes?")
            if response is None:  # Cancel
                return False
            if response:
                saved = self._save_file()
                return saved
        return True

    def _exit(self):
        if self._maybe_save():
            self.root.destroy()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = NotepadApp()
    app.run()

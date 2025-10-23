import gi
import subprocess
import threading
import json
import os
import re
import time
import glob
import signal
from collections import defaultdict
import asyncio
from concurrent.futures import ThreadPoolExecutor

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

class PackageInfo:
    def __init__(self, name="", version="", description="", repo="", size="", 
                 installed_size="", depends="", url="", licenses="", groups="",
                 provides="", conflicts="", replaces="", installed=False, update_available=False):
        self.name = name
        self.version = version
        self.description = description
        self.repo = repo
        self.size = size
        self.installed_size = installed_size
        self.depends = depends
        self.url = url
        self.licenses = licenses
        self.groups = groups
        self.provides = provides
        self.conflicts = conflicts
        self.replaces = replaces
        self.installed = installed
        self.update_available = update_available
        self.relevance_score = 0

class PackageQueue:
    def __init__(self):
        self.packages = []
        self.callbacks = []
        self._lock = threading.Lock()
    
    def add_package(self, package):
        with self._lock:
            if not any(p.name == package.name for p in self.packages):
                self.packages.append(package)
                self.notify_callbacks()
    
    def remove_package(self, package):
        with self._lock:
            self.packages = [p for p in self.packages if p.name != package.name]
            self.notify_callbacks()
    
    def clear(self):
        with self._lock:
            self.packages = []
            self.notify_callbacks()
    
    def add_callback(self, callback):
        self.callbacks.append(callback)
    
    def notify_callbacks(self):
        for callback in self.callbacks:
            GLib.idle_add(callback)
    
    def get_separated_packages(self):
        with self._lock:
            pacman_packages = []
            aur_packages = []
            
            for package in self.packages:
                if package.repo.lower() == "aur" or package.repo.lower() == "popular":
                    aur_packages.append(package)
                else:
                    pacman_packages.append(package)
            
            return pacman_packages, aur_packages

class AsyncOperation:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    def run_async(self, func, *args, callback=None, **kwargs):
        def wrapper():
            try:
                result = func(*args, **kwargs)
                if callback:
                    GLib.idle_add(callback, result, None)
            except Exception as e:
                if callback:
                    GLib.idle_add(callback, None, e)
        
        self.executor.submit(wrapper)
    
    def shutdown(self):
        self.executor.shutdown(wait=False)

async_op = AsyncOperation()

class PackageRow(Gtk.ListBoxRow):
    def __init__(self, package, queue=None):
        super().__init__()
        self.package = package
        self.queue = queue
        
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        
        icon = self.get_package_icon(package.name)
        icon.set_pixel_size(32)
        box.append(icon)
        
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_hexpand(True)
        
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label=package.name)
        name_label.set_xalign(0)
        name_label.add_css_class("heading")
        name_box.append(name_label)
        
        if package.installed:
            badge = Gtk.Label(label="Installed")
            badge.add_css_class("success")
            badge.add_css_class("caption")
            name_box.append(badge)
        
        if package.update_available:
            update_badge = Gtk.Label(label="Update")
            update_badge.add_css_class("accent")
            update_badge.add_css_class("caption")
            name_box.append(update_badge)
        
        info_box.append(name_box)
        
        desc_label = Gtk.Label(label=package.description[:80] + "..." if len(package.description) > 80 else package.description)
        desc_label.set_xalign(0)
        desc_label.add_css_class("dim-label")
        desc_label.add_css_class("caption")
        info_box.append(desc_label)
        
        version_label = Gtk.Label(label=f"{package.version} • {package.repo}")
        version_label.set_xalign(0)
        version_label.add_css_class("caption")
        info_box.append(version_label)
        
        box.append(info_box)
        
        if queue and not package.installed:
            queue_button = Gtk.Button(icon_name="list-add-symbolic")
            queue_button.set_tooltip_text("Add to queue")
            queue_button.add_css_class("flat")
            queue_button.connect("clicked", self.on_queue_clicked)
            box.append(queue_button)
        
        self.set_child(box)
    
    def get_package_icon(self, package_name):
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        
        if icon_theme.has_icon(package_name):
            return Gtk.Image.new_from_icon_name(package_name)
        
        variations = [
            package_name,
            package_name.replace('-', '_'),
            package_name.lower(),
            package_name.replace('.desktop', ''),
            f"org.{package_name}",
            f"com.{package_name}",
            f"io.{package_name}",
            f"app.{package_name}",
        ]
        
        for var in variations:
            if icon_theme.has_icon(var):
                return Gtk.Image.new_from_icon_name(var)
        
        icon_paths = [
            f"/usr/share/icons/hicolor/64x64/apps/{package_name}.png",
            f"/usr/share/icons/hicolor/64x64/apps/{package_name}.svg",
            f"/usr/share/icons/hicolor/48x48/apps/{package_name}.png",
            f"/usr/share/icons/hicolor/48x48/apps/{package_name}.svg",
            f"/usr/share/pixmaps/{package_name}.png",
            f"/usr/share/pixmaps/{package_name}.svg",
        ]
        
        for var in variations:
            for path in icon_paths:
                test_path = path.replace(package_name, var)
                if os.path.exists(test_path):
                    try:
                        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(test_path, 32, 32, True)
                        return Gtk.Image.new_from_pixbuf(pixbuf)
                    except:
                        pass
        
        desktop_paths = [
            f"/usr/share/applications/{package_name}.desktop",
            f"/usr/share/applications/{package_name.lower()}.desktop",
            f"/usr/share/applications/org.{package_name}.desktop",
            f"/usr/share/applications/com.{package_name}.desktop",
        ]
        
        for desktop_path in desktop_paths:
            if os.path.exists(desktop_path):
                try:
                    with open(desktop_path, 'r') as f:
                        for line in f:
                            if line.startswith('Icon='):
                                icon_name = line.strip().split('=', 1)[1]
                                if icon_theme.has_icon(icon_name):
                                    return Gtk.Image.new_from_icon_name(icon_name)
                                if os.path.exists(icon_name):
                                    try:
                                        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_name, 32, 32, True)
                                        return Gtk.Image.new_from_pixbuf(pixbuf)
                                    except:
                                        pass
                except:
                    pass
        
        category_icons = {
            "firefox": "web-browser",
            "chromium": "web-browser",
            "vlc": "multimedia-player",
            "mpv": "multimedia-player",
            "gimp": "image-x-generic",
            "libreoffice": "x-office-document",
            "code": "text-editor",
            "git": "terminal",
            "python": "text-x-script",
            "nodejs": "text-x-script",
            "docker": "container",
            "steam": "applications-games",
            "lutris": "applications-games",
            "wine": "applications-games",
            "htop": "utilities-system-monitor",
            "neofetch": "terminal",
            "gparted": "utilities-disk-utility",
            "timeshift": "document-save",
        }
        
        for key, icon in category_icons.items():
            if key in package_name.lower():
                return Gtk.Image.new_from_icon_name(icon)
        
        return Gtk.Image.new_from_icon_name("package-x-generic")
    
    def on_queue_clicked(self, button):
        if self.queue:
            self.queue.add_package(self.package)

class InstallDialog(Adw.MessageDialog):
    def __init__(self, parent, package, is_aur=False):
        super().__init__(transient_for=parent)
        self.package = package
        self.is_aur = is_aur
        
        self.set_heading(f"Install {package.name}")
        self.set_body(f"Do you want to install {package.name}?")
        
        self.add_response("cancel", "Cancel")
        self.add_response("install", "Install")
        self.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("install")
        self.set_close_response("cancel")
        
        self.connect("response", self.on_response)
    
    def on_response(self, dialog, response):
        if response == "install":
            window = self.get_transient_for()
            window.show_install_progress(self.package, self.is_aur)

class UninstallDialog(Adw.MessageDialog):
    def __init__(self, parent, package):
        super().__init__(transient_for=parent)
        self.package = package
        
        self.set_heading(f"Uninstall {package.name}")
        self.set_body(f"Are you sure you want to uninstall {package.name}? This will remove the package and its dependencies that are no longer needed.")
        
        self.add_response("cancel", "Cancel")
        self.add_response("uninstall", "Uninstall")
        self.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        self.set_default_response("cancel")
        self.set_close_response("cancel")
        
        self.connect("response", self.on_response)
    
    def on_response(self, dialog, response):
        if response == "uninstall":
            window = self.get_transient_for()
            window.show_uninstall_progress(self.package)

class UpdateDialog(Adw.MessageDialog):
    def __init__(self, parent, package=None, is_system=False):
        super().__init__(transient_for=parent)
        self.package = package
        self.is_system = is_system
        
        if is_system:
            self.set_heading("Update System")
            self.set_body("Do you want to update all packages? This will upgrade all installed packages to their latest versions.")
        else:
            self.set_heading(f"Update {package.name}")
            self.set_body(f"Do you want to update {package.name} to the latest version?")
        
        self.add_response("cancel", "Cancel")
        self.add_response("update", "Update")
        self.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("update")
        self.set_close_response("cancel")
        
        self.connect("response", self.on_response)
    
    def on_response(self, dialog, response):
        if response == "update":
            window = self.get_transient_for()
            if self.is_system:
                window.show_system_update_progress()
            else:
                window.show_update_progress(self.package)

class QueueInstallDialog(Adw.MessageDialog):
    def __init__(self, parent, queue):
        super().__init__(transient_for=parent)
        self.queue = queue
        
        pacman_packages, aur_packages = queue.get_separated_packages()
        
        body_text = f"Do you want to install {len(queue.packages)} packages from the queue?"
        if pacman_packages and aur_packages:
            body_text += f"\n\n{len(pacman_packages)} from official repositories and {len(aur_packages)} from AUR"
        elif aur_packages:
            body_text += f"\n\nAll packages are from AUR"
        else:
            body_text += f"\n\nAll packages are from official repositories"
        
        self.set_heading("Install Queue")
        self.set_body(body_text)
        
        self.add_response("cancel", "Cancel")
        self.add_response("install", "Install All")
        self.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("install")
        self.set_close_response("cancel")
        
        self.connect("response", self.on_response)
    
    def on_response(self, dialog, response):
        if response == "install":
            window = self.get_transient_for()
            window.show_queue_install_progress()

class AuthenticationErrorDialog(Adw.MessageDialog):
    def __init__(self, parent):
        super().__init__(transient_for=parent)
        
        self.set_heading("Authentication Required")
        self.set_body("This operation requires administrator privileges. Please ensure:\n\n1. You have sudo/polkit configured properly\n2. An authentication agent is running\n3. You're in the sudoers file\n\nYou can also install packages from terminal using:\nsudo pacman -S <package-name>\nyay -S <package-name>")
        
        self.add_response("ok", "OK")
        self.set_default_response("ok")
        self.set_close_response("ok")

class InstallProgressWindow(Adw.Window):
    def __init__(self, parent, package=None, is_aur=False, operation="install", queue=None):
        super().__init__(transient_for=parent)
        self.set_default_size(700, 500)
        
        if operation == "system_update":
            self.set_title("Updating System")
        elif operation == "queue_install":
            self.set_title("Installing Queue")
        elif package:
            self.set_title(f"{operation.capitalize()} {package.name}")
        
        self.package = package
        self.is_aur = is_aur
        self.operation = operation
        self.queue = queue
        self.process = None
        self.cancelled = False
        self.completed = False
        self.current_percentage = 0
        self.last_log_line = ""
        self.download_total = 0
        self.download_current = 0
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        headerbar = Adw.HeaderBar()
        content.append(headerbar)
        
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        progress_box.set_margin_start(12)
        progress_box.set_margin_end(12)
        progress_box.set_margin_top(12)
        progress_box.set_margin_bottom(12)
        
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_text("0%")
        progress_box.append(self.progress_bar)
        
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.status_label = Gtk.Label(label="Preparing...")
        self.status_label.set_hexpand(True)
        self.status_label.set_xalign(0)
        self.percentage_label = Gtk.Label(label="0%")
        self.percentage_label.add_css_class("heading")
        status_box.append(self.status_label)
        status_box.append(self.percentage_label)
        progress_box.append(status_box)
        
        self.operation_label = Gtk.Label(label="")
        self.operation_label.add_css_class("caption")
        self.operation_label.add_css_class("dim-label")
        self.operation_label.set_xalign(0)
        progress_box.append(self.operation_label)
        
        content.append(progress_box)
        
        self.expander = Gtk.Expander(label="Show details")
        self.expander.set_margin_start(12)
        self.expander.set_margin_end(12)
        self.expander.set_margin_top(6)
        self.expander.set_margin_bottom(6)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(200)
        
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_margin_start(12)
        self.textview.set_margin_end(12)
        self.textview.set_margin_top(12)
        self.textview.set_margin_bottom(12)
        
        self.buffer = self.textview.get_buffer()
        scrolled.set_child(self.textview)
        self.expander.set_child(scrolled)
        content.append(self.expander)
        
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(12)
        button_box.set_margin_end(12)
        button_box.set_margin_top(6)
        button_box.set_margin_bottom(12)
        button_box.set_halign(Gtk.Align.END)
        
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self.on_cancel_clicked)
        button_box.append(self.cancel_button)
        
        self.close_button = Gtk.Button(label="Close")
        self.close_button.connect("clicked", lambda b: self.close())
        self.close_button.set_sensitive(False)
        self.close_button.set_visible(False)
        button_box.append(self.close_button)
        
        self.open_button = Gtk.Button(label="Open")
        self.open_button.add_css_class("suggested-action")
        self.open_button.connect("clicked", self.on_open_clicked)
        self.open_button.set_sensitive(False)
        self.open_button.set_visible(False)
        button_box.append(self.open_button)
        
        content.append(button_box)
        
        self.set_content(content)
        
        threading.Thread(target=self.execute_operation, daemon=True).start()
    
    def append_output(self, text):
        GLib.idle_add(self._append_output_ui, text)
    
    def _append_output_ui(self, text):
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text)
        
        mark = self.buffer.create_mark(None, end_iter, False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
    
    def update_progress(self, fraction, status, operation=""):
        GLib.idle_add(self._update_progress_ui, fraction, status, operation)
    
    def _update_progress_ui(self, fraction, status, operation):
        self.current_percentage = int(fraction * 100)
        self.progress_bar.set_fraction(fraction)
        self.progress_bar.set_text(f"{self.current_percentage}%")
        self.status_label.set_label(status)
        self.percentage_label.set_label(f"{self.current_percentage}%")
        if operation:
            self.operation_label.set_label(operation)
    
    def on_cancel_clicked(self, button):
        self.cancelled = True
        self.append_output("\n\n--- CANCELLING INSTALLATION ---\n\n")
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        
        self.cancel_button.set_sensitive(False)
        self.close_button.set_sensitive(True)
        self.close_button.set_visible(True)
        self.update_progress(1.0, "Installation cancelled", "")
    
    def on_open_clicked(self, button):
        if self.package:
            desktop_paths = [
                f"/usr/share/applications/{self.package.name}.desktop",
                f"/usr/share/applications/{self.package.name.lower()}.desktop",
                f"/usr/share/applications/org.{self.package.name}.desktop",
            ]
            
            for desktop_path in desktop_paths:
                if os.path.exists(desktop_path):
                    try:
                        subprocess.Popen(["gtk-launch", os.path.basename(desktop_path)])
                        return
                    except:
                        pass
            
            try:
                subprocess.Popen([self.package.name])
            except:
                pass
    
    def execute_operation(self):
        try:
            if self.operation == "install":
                self.update_progress(0.05, "Initializing installation...", "Resolving dependencies...")
                if self.is_aur:
                    cmd = ['yay', '-S', '--noconfirm', self.package.name]
                else:
                    cmd = ['pkexec', 'pacman', '-S', '--noconfirm', self.package.name]
            elif self.operation == "uninstall":
                self.update_progress(0.05, "Initializing uninstallation...", "Checking dependencies...")
                cmd = ['pkexec', 'pacman', '-R', '--noconfirm', self.package.name]
            elif self.operation == "update":
                self.update_progress(0.05, "Initializing update...", "Checking for updates...")
                if self.is_aur:
                    cmd = ['yay', '-S', '--noconfirm', self.package.name]
                else:
                    cmd = ['pkexec', 'pacman', '-S', '--noconfirm', self.package.name]
            elif self.operation == "system_update":
                self.update_progress(0.05, "Initializing system update...", "Checking for updates...")
                cmd = ['pkexec', 'pacman', '-Syu', '--noconfirm']
            elif self.operation == "queue_install":
                self.execute_queue_install()
                return
            
            self.append_output(f"Running: {' '.join(cmd)}\n\n")
            
            env = os.environ.copy()
            env['LC_ALL'] = 'C'
            
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None
            )
            
            try:
                self.process.stdin.write("Y\n")
                self.process.stdin.flush()
                time.sleep(0.5)
                self.process.stdin.write("Y\n")
                self.process.stdin.flush()
            except:
                pass
            
            total_lines = 0
            last_progress_time = time.time()
            progress_pattern = re.compile(r'\((\d+)%\)')
            download_pattern = re.compile(r'downloading.*\((\d+)%\)', re.IGNORECASE)
            installing_pattern = re.compile(r'installing.*\((\d+)%\)', re.IGNORECASE)
            download_size_pattern = re.compile(r'(\d+\.?\d*[KMGT]?iB)\/(\d+\.?\d*[KMGT]?iB)', re.IGNORECASE)
            
            while True:
                if self.cancelled:
                    break
                
                try:
                    line = self.process.stdout.readline()
                    if not line:
                        break
                except:
                    break
                
                self.append_output(line)
                total_lines += 1
                self.last_log_line = line.strip()
                current_time = time.time()
                
                if "sudo: a password is required" in line or "sudo: a terminal is required" in line:
                    self.update_progress(1.0, "Authentication failed", "Check your sudo/polkit configuration")
                    self.append_output("\n✗ Authentication failed. Please check your system configuration.\n")
                    self.process.terminate()
                    GLib.idle_add(self.show_authentication_error)
                    break
                
                match = progress_pattern.search(line)
                if match:
                    percent = int(match.group(1)) / 100.0
                    if "downloading" in line.lower():
                        self.update_progress(0.1 + percent * 0.3, "Downloading...", self.last_log_line)
                    elif "installing" in line.lower():
                        self.update_progress(0.4 + percent * 0.5, "Installing...", self.last_log_line)
                    else:
                        self.update_progress(0.1 + percent * 0.8, "Processing...", self.last_log_line)
                    last_progress_time = current_time
                else:
                    size_match = download_size_pattern.search(line)
                    if size_match:
                        try:
                            current_str = size_match.group(1)
                            total_str = size_match.group(2)
                            
                            def parse_size(size_str):
                                size_str = size_str.upper()
                                if size_str.endswith('KIB') or size_str.endswith('KB'):
                                    return float(size_str[:-3]) * 1024
                                elif size_str.endswith('MIB') or size_str.endswith('MB'):
                                    return float(size_str[:-3]) * 1024 * 1024
                                elif size_str.endswith('GIB') or size_str.endswith('GB'):
                                    return float(size_str[:-3]) * 1024 * 1024 * 1024
                                else:
                                    return float(size_str.replace('IB', '').replace('B', ''))
                            
                            current_size = parse_size(current_str)
                            total_size = parse_size(total_str)
                            
                            if total_size > 0:
                                percent = current_size / total_size
                                self.update_progress(0.1 + percent * 0.3, "Downloading...", f"Downloaded {current_str} of {total_str}")
                                last_progress_time = current_time
                        except:
                            pass
                    else:
                        line_lower = line.lower()
                        if "resolving dependencies" in line_lower:
                            self.update_progress(0.05, "Resolving dependencies...", line.strip())
                            last_progress_time = current_time
                        elif "checking dependencies" in line_lower:
                            self.update_progress(0.1, "Checking dependencies...", line.strip())
                            last_progress_time = current_time
                        elif "retrieving packages" in line_lower:
                            self.update_progress(0.15, "Retrieving packages...", line.strip())
                            last_progress_time = current_time
                        elif "checking keyring" in line_lower:
                            self.update_progress(0.2, "Checking package integrity...", line.strip())
                            last_progress_time = current_time
                        elif "loading package files" in line_lower:
                            self.update_progress(0.25, "Loading packages...", line.strip())
                            last_progress_time = current_time
                        elif "checking for conflicts" in line_lower:
                            self.update_progress(0.3, "Checking for conflicts...", line.strip())
                            last_progress_time = current_time
                        elif "checking available disk space" in line_lower:
                            self.update_progress(0.35, "Checking disk space...", line.strip())
                            last_progress_time = current_time
                        elif "running pre-transaction hooks" in line_lower:
                            self.update_progress(0.4, "Running pre-transaction hooks...", line.strip())
                            last_progress_time = current_time
                        elif "processing package changes" in line_lower:
                            self.update_progress(0.45, "Processing package changes...", line.strip())
                            last_progress_time = current_time
                        elif "running post-transaction hooks" in line_lower:
                            self.update_progress(0.9, "Running post-transaction hooks...", line.strip())
                            last_progress_time = current_time
                        elif ":: proceed with installation?" in line_lower:
                            try:
                                self.process.stdin.write("Y\n")
                                self.process.stdin.flush()
                            except:
                                pass
                        elif current_time - last_progress_time > 2:
                            progress = min(0.1 + (total_lines * 0.001), 0.95)
                            self.update_progress(progress, "Processing...", self.last_log_line)
                            last_progress_time = current_time
            
            if not self.cancelled:
                self.process.wait()
            
            if self.cancelled:
                self.update_progress(1.0, "Installation cancelled", "")
                self.append_output(f"\n✗ Installation cancelled\n")
            elif self.process.returncode == 0:
                self.completed = True
                if self.operation == "install":
                    self.update_progress(1.0, f"✓ {self.package.name} installed successfully!", "Installation completed")
                    self.append_output(f"\n✓ {self.package.name} installed successfully!\n")
                elif self.operation == "uninstall":
                    self.update_progress(1.0, f"✓ {self.package.name} uninstalled successfully!", "Uninstallation completed")
                    self.append_output(f"\n✓ {self.package.name} uninstalled successfully!\n")
                elif self.operation == "update":
                    self.update_progress(1.0, f"✓ {self.package.name} updated successfully!", "Update completed")
                    self.append_output(f"\n✓ {self.package.name} updated successfully!\n")
                elif self.operation == "system_update":
                    self.update_progress(1.0, "✓ System updated successfully!", "System update completed")
                    self.append_output(f"\n✓ System updated successfully!\n")
            else:
                if self.operation == "system_update":
                    self.update_progress(1.0, "✗ Failed to update system", "Update failed")
                    self.append_output(f"\n✗ Failed to update system\n")
                else:
                    self.update_progress(1.0, f"✗ Failed to {self.operation} {self.package.name}", "Operation failed")
                    self.append_output(f"\n✗ Failed to {self.operation} {self.package.name}\n")
            
        except Exception as e:
            self.update_progress(1.0, f"✗ Error: {str(e)}", "Error occurred")
            self.append_output(f"\n✗ Error: {str(e)}\n")
        
        finally:
            GLib.idle_add(self.operation_complete)
    
    def show_authentication_error(self):
        dialog = AuthenticationErrorDialog(self)
        dialog.present()
    
    def execute_queue_install(self):
        pacman_packages, aur_packages = self.queue.get_separated_packages()
        
        try:
            total_packages = len(pacman_packages) + len(aur_packages)
            current_progress = 0.05
            
            if pacman_packages:
                self.update_progress(current_progress, "Installing official packages...", f"Installing {len(pacman_packages)} packages from repositories")
                package_names = [p.name for p in pacman_packages]
                cmd = ['pkexec', 'pacman', '-S', '--noconfirm'] + package_names
                
                self.append_output(f"Installing official packages: {' '.join(package_names)}\n")
                self.append_output(f"Running: {' '.join(cmd)}\n\n")
                
                env = os.environ.copy()
                env['LC_ALL'] = 'C'
                
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                )
                
                try:
                    process.stdin.write("Y\n")
                    process.stdin.flush()
                    time.sleep(0.5)
                    process.stdin.write("Y\n")
                    process.stdin.flush()
                except:
                    pass
                
                last_progress_time = time.time()
                while True:
                    if self.cancelled:
                        break
                    
                    try:
                        line = process.stdout.readline()
                        if not line:
                            break
                    except:
                        break
                    
                    self.append_output(line)
                    current_time = time.time()
                    
                    if "sudo: a password is required" in line or "sudo: a terminal is required" in line:
                        self.update_progress(1.0, "Authentication failed", "Check your sudo/polkit configuration")
                        self.append_output("\n✗ Authentication failed. Please check your system configuration.\n")
                        process.terminate()
                        GLib.idle_add(self.show_authentication_error)
                        break
                    
                    if ":: proceed with installation?" in line.lower():
                        try:
                            process.stdin.write("Y\n")
                            process.stdin.flush()
                        except:
                            pass
                    
                    if current_time - last_progress_time > 3:
                        progress = min(current_progress + 0.01, 0.5)
                        self.update_progress(progress, "Installing official packages...", line.strip())
                        last_progress_time = current_time
                
                if not self.cancelled:
                    process.wait()
                    
                    if process.returncode != 0:
                        self.update_progress(1.0, "✗ Failed to install official packages", "Installation failed")
                        self.append_output(f"\n✗ Failed to install official packages\n")
                        GLib.idle_add(self.operation_complete)
                        return
                
                current_progress += 0.45
            
            if aur_packages and not self.cancelled:
                self.update_progress(current_progress, "Installing AUR packages...", f"Installing {len(aur_packages)} packages from AUR")
                package_names = [p.name for p in aur_packages]
                cmd = ['yay', '-S', '--noconfirm'] + package_names
                
                self.append_output(f"\nInstalling AUR packages: {' '.join(package_names)}\n")
                self.append_output(f"Running: {' '.join(cmd)}\n\n")
                
                env = os.environ.copy()
                env['LC_ALL'] = 'C'
                
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                )
                
                try:
                    process.stdin.write("Y\n")
                    process.stdin.flush()
                    time.sleep(0.5)
                    process.stdin.write("Y\n")
                    process.stdin.flush()
                except:
                    pass
                
                last_progress_time = time.time()
                while True:
                    if self.cancelled:
                        break
                    
                    try:
                        line = process.stdout.readline()
                        if not line:
                            break
                    except:
                        break
                    
                    self.append_output(line)
                    current_time = time.time()
                    
                    if "sudo: a password is required" in line or "sudo: a terminal is required" in line:
                        self.update_progress(1.0, "Authentication failed", "Check your sudo/polkit configuration")
                        self.append_output("\n✗ Authentication failed. Please check your system configuration.\n")
                        process.terminate()
                        GLib.idle_add(self.show_authentication_error)
                        break
                    
                    if ":: proceed with installation?" in line.lower():
                        try:
                            process.stdin.write("Y\n")
                            process.stdin.flush()
                        except:
                            pass
                    
                    if current_time - last_progress_time > 3:
                        progress = min(current_progress + 0.01, 0.95)
                        self.update_progress(progress, "Installing AUR packages...", line.strip())
                        last_progress_time = current_time
                
                if not self.cancelled:
                    process.wait()
                    
                    if process.returncode != 0:
                        self.update_progress(1.0, "✗ Failed to install AUR packages", "Installation failed")
                        self.append_output(f"\n✗ Failed to install AUR packages\n")
                        GLib.idle_add(self.operation_complete)
                        return
            
            if not self.cancelled:
                self.completed = True
                self.update_progress(1.0, "✓ Queue installed successfully!", "All packages installed")
                self.append_output(f"\n✓ All {total_packages} packages installed successfully!\n")
                self.queue.clear()
            else:
                self.update_progress(1.0, "Installation cancelled", "")
                self.append_output(f"\n✗ Installation cancelled\n")
        
        except Exception as e:
            self.update_progress(1.0, f"✗ Error: {str(e)}", "Error occurred")
            self.append_output(f"\n✗ Error: {str(e)}\n")
        
        finally:
            GLib.idle_add(self.operation_complete)
    
    def operation_complete(self):
        self.cancel_button.set_sensitive(False)
        self.cancel_button.set_visible(False)
        self.close_button.set_sensitive(True)
        self.close_button.set_visible(True)
        
        if self.completed and self.operation in ["install", "update"] and self.package:
            self.open_button.set_sensitive(True)
            self.open_button.set_visible(True)

class PackageDetailPage(Gtk.Box):
    def __init__(self, package, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.package = package
        self.window = window
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        
        name_label = Gtk.Label(label=package.name)
        name_label.add_css_class("title-1")
        name_label.set_xalign(0)
        content.append(name_label)
        
        version_label = Gtk.Label(label=f"Version: {package.version}")
        version_label.add_css_class("title-4")
        version_label.add_css_class("dim-label")
        version_label.set_xalign(0)
        content.append(version_label)
        
        if package.description:
            desc_label = Gtk.Label(label=package.description)
            desc_label.set_wrap(True)
            desc_label.set_xalign(0)
            desc_label.set_margin_top(12)
            content.append(desc_label)
        
        info_group = Adw.PreferencesGroup()
        info_group.set_title("Information")
        info_group.set_margin_top(24)
        
        if package.repo:
            repo_row = Adw.ActionRow(title="Repository")
            repo_row.add_suffix(Gtk.Label(label=package.repo))
            info_group.add(repo_row)
        
        if package.size:
            size_row = Adw.ActionRow(title="Download Size")
            size_row.add_suffix(Gtk.Label(label=package.size))
            info_group.add(size_row)
        
        if package.installed_size:
            installed_size_row = Adw.ActionRow(title="Installed Size")
            installed_size_row.add_suffix(Gtk.Label(label=package.installed_size))
            info_group.add(installed_size_row)
        
        if package.licenses:
            license_row = Adw.ActionRow(title="License")
            license_row.add_suffix(Gtk.Label(label=package.licenses))
            info_group.add(license_row)
        
        if package.url:
            url_row = Adw.ActionRow(title="Website")
            url_label = Gtk.Label(label=package.url)
            url_label.add_css_class("dim-label")
            url_row.add_suffix(url_label)
            info_group.add(url_row)
        
        content.append(info_group)
        
        if package.depends:
            deps_group = Adw.PreferencesGroup()
            deps_group.set_title("Dependencies")
            deps_group.set_margin_top(12)
            
            deps_label = Gtk.Label(label=package.depends)
            deps_label.set_wrap(True)
            deps_label.set_xalign(0)
            deps_label.add_css_class("dim-label")
            deps_label.set_margin_start(12)
            deps_label.set_margin_end(12)
            deps_label.set_margin_top(6)
            deps_label.set_margin_bottom(6)
            
            deps_row = Adw.PreferencesRow()
            deps_row.set_child(deps_label)
            deps_group.add(deps_row)
            content.append(deps_group)
        
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_top(24)
        button_box.set_halign(Gtk.Align.CENTER)
        
        if not package.installed:
            install_button = Gtk.Button(label="Install")
            install_button.add_css_class("suggested-action")
            install_button.add_css_class("pill")
            install_button.connect("clicked", self.on_install_clicked)
            button_box.append(install_button)
            
            queue_button = Gtk.Button(label="Add to Queue")
            queue_button.add_css_class("pill")
            queue_button.connect("clicked", self.on_queue_clicked)
            button_box.append(queue_button)
        else:
            if package.update_available:
                update_button = Gtk.Button(label="Update")
                update_button.add_css_class("suggested-action")
                update_button.add_css_class("pill")
                update_button.connect("clicked", self.on_update_clicked)
                button_box.append(update_button)
            
            uninstall_button = Gtk.Button(label="Uninstall")
            uninstall_button.add_css_class("destructive-action")
            uninstall_button.add_css_class("pill")
            uninstall_button.connect("clicked", self.on_uninstall_clicked)
            button_box.append(uninstall_button)
        
        content.append(button_box)
        
        scrolled.set_child(content)
        self.append(scrolled)
    
    def on_install_clicked(self, button):
        is_aur = self.package.repo.lower() == "aur"
        dialog = InstallDialog(self.window, self.package, is_aur)
        dialog.present()
    
    def on_queue_clicked(self, button):
        self.window.queue.add_package(self.package)
        self.window.add_toast(f"Added {self.package.name} to queue")
    
    def on_uninstall_clicked(self, button):
        dialog = UninstallDialog(self.window, self.package)
        dialog.present()
    
    def on_update_clicked(self, button):
        is_aur = self.package.repo.lower() == "aur"
        dialog = UpdateDialog(self.window, self.package, is_aur)
        dialog.present()

class QuickInstallPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_valign(Gtk.Align.CENTER)
        content.set_halign(Gtk.Align.CENTER)
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "dastore.png")
        
        if os.path.exists(icon_path):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 256, 256, True)
                app_icon = Gtk.Image.new_from_pixbuf(pixbuf)
                content.append(app_icon)
            except:
                pass
        
        title = Gtk.Label(label="Quick Install")
        title.add_css_class("title-1")
        title.set_margin_bottom(24)
        content.append(title)
        
        entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        entry_box.set_size_request(500, -1)
        
        self.package_entry = Gtk.Entry()
        self.package_entry.set_placeholder_text("Enter package name...")
        self.package_entry.set_hexpand(True)
        self.package_entry.connect("activate", self.on_install_clicked)
        entry_box.append(self.package_entry)
        
        install_button = Gtk.Button(label="Install")
        install_button.add_css_class("suggested-action")
        install_button.add_css_class("pill")
        install_button.connect("clicked", self.on_install_clicked)
        entry_box.append(install_button)
        
        content.append(entry_box)
        
        self.status_label = Gtk.Label(label="")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_margin_top(12)
        content.append(self.status_label)
        
        self.append(content)
    
    def on_install_clicked(self, button):
        package_name = self.package_entry.get_text().strip()
        if not package_name:
            self.status_label.set_label("Please enter a package name")
            return
        
        self.status_label.set_label(f"Installing {package_name}...")
        package = PackageInfo(name=package_name, repo="quick")
        dialog = InstallDialog(self.window, package, is_aur=False)
        dialog.present()
        self.package_entry.set_text("")
        self.status_label.set_label("")

class HistoryPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        
        title = Gtk.Label(label="Installation History")
        title.add_css_class("title-1")
        title.set_xalign(0)
        content.append(title)
        
        history_group = Adw.PreferencesGroup()
        history_group.set_title("Recently Installed Packages")
        history_group.set_margin_top(24)
        
        history = self.get_install_history()
        if history:
            for item in history:
                row = Adw.ActionRow(title=item['name'])
                row.set_subtitle(f"Installed on {item['date']}")
                history_group.add(row)
        else:
            row = Adw.ActionRow(title="No history found")
            history_group.add(row)
        
        content.append(history_group)
        
        scrolled.set_child(content)
        self.append(scrolled)
    
    def get_install_history(self):
        history = []
        try:
            log_file = "/var/log/pacman.log"
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    lines = f.readlines()
                    for line in reversed(lines[-100:]):
                        if "installed" in line and "[" in line:
                            try:
                                date_str = line.split("[")[1].split("]")[0]
                                package = line.split("installed")[1].split("(")[0].strip()
                                if package:
                                    history.append({
                                        'name': package,
                                        'date': date_str
                                    })
                                    if len(history) >= 20:
                                        break
                            except:
                                continue
        except:
            pass
        return history

class SizeSortPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        
        title = Gtk.Label(label="Packages by Size")
        title.add_css_class("title-1")
        title.set_xalign(0)
        content.append(title)
        
        self.status_label = Gtk.Label(label="Loading package sizes...")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_margin_top(12)
        content.append(self.status_label)
        
        size_group = Adw.PreferencesGroup()
        size_group.set_title("Largest Packages")
        size_group.set_margin_top(24)
        
        scrolled.set_child(content)
        self.append(scrolled)
        
        threading.Thread(target=self.load_package_sizes, args=(size_group,), daemon=True).start()
    
    def load_package_sizes(self, size_group):
        try:
            result = subprocess.run(['pacman', '-Qi'], 
                                  capture_output=True, text=True, timeout=30)
            packages = self.parse_package_sizes(result.stdout)
            
            GLib.idle_add(self.update_size_list, size_group, packages)
        except:
            GLib.idle_add(self.status_label.set_label, "Failed to load package sizes")
    
    def parse_package_sizes(self, output):
        packages = []
        current_pkg = {}
        
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('Name'):
                if current_pkg:
                    packages.append(current_pkg)
                current_pkg = {'name': line.split(':')[1].strip()}
            elif line.startswith('Installed Size'):
                size_str = line.split(':')[1].strip()
                current_pkg['size'] = size_str
                current_pkg['size_bytes'] = self.parse_size_to_bytes(size_str)
        
        if current_pkg:
            packages.append(current_pkg)
        
        packages.sort(key=lambda x: x['size_bytes'], reverse=True)
        return packages[:20]
    
    def parse_size_to_bytes(self, size_str):
        size_str = size_str.upper()
        if 'KIB' in size_str:
            return float(size_str.replace('KIB', '').strip()) * 1024
        elif 'MIB' in size_str:
            return float(size_str.replace('MIB', '').strip()) * 1024 * 1024
        elif 'GIB' in size_str:
            return float(size_str.replace('GIB', '').strip()) * 1024 * 1024 * 1024
        return 0
    
    def update_size_list(self, size_group, packages):
        self.status_label.set_visible(False)
        
        for pkg in packages:
            row = Adw.ActionRow(title=pkg['name'])
            row.set_subtitle(pkg['size'])
            size_group.add(row)

class QueuePage(Gtk.Box):
    def __init__(self, queue):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.queue = queue
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        
        title = Gtk.Label(label="Install Queue")
        title.add_css_class("title-1")
        title.set_xalign(0)
        content.append(title)
        
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(24)
        self.listbox.connect("row-activated", self.on_package_selected)
        
        scrolled.set_child(self.listbox)
        content.append(scrolled)
        
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_top(24)
        button_box.set_halign(Gtk.Align.CENTER)
        
        self.install_button = Gtk.Button(label="Install All")
        self.install_button.add_css_class("suggested-action")
        self.install_button.add_css_class("pill")
        self.install_button.connect("clicked", self.on_install_clicked)
        self.install_button.set_sensitive(False)
        button_box.append(self.install_button)
        
        clear_button = Gtk.Button(label="Clear Queue")
        clear_button.add_css_class("pill")
        clear_button.connect("clicked", self.on_clear_clicked)
        clear_button.set_sensitive(False)
        button_box.append(clear_button)
        
        content.append(button_box)
        
        self.append(content)
        
        self.queue.add_callback(self.update_queue)
        self.update_queue()
    
    def update_queue(self):
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        
        for package in self.queue.packages:
            row = PackageRow(package, self.queue)
            self.listbox.append(row)
        
        self.install_button.set_sensitive(len(self.queue.packages) > 0)
    
    def on_package_selected(self, listbox, row):
        package = row.package
        self.queue.remove_package(package)
    
    def on_install_clicked(self, button):
        if len(self.queue.packages) > 0:
            dialog = QueueInstallDialog(self.get_root(), self.queue)
            dialog.present()
    
    def on_clear_clicked(self, button):
        self.queue.clear()

class RepositoriesPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        
        title = Gtk.Label(label="Manage Repositories")
        title.add_css_class("title-1")
        title.set_xalign(0)
        content.append(title)
        
        repos_group = Adw.PreferencesGroup()
        repos_group.set_title("Active Repositories")
        repos_group.set_margin_top(24)
        
        repos = self.get_repositories()
        for repo in repos:
            row = Adw.ActionRow(title=repo)
            switch = Gtk.Switch()
            switch.set_active(True)
            switch.set_valign(Gtk.Align.CENTER)
            row.add_suffix(switch)
            repos_group.add(row)
        
        content.append(repos_group)
        
        update_button = Gtk.Button(label="Update Package List")
        update_button.set_margin_top(24)
        update_button.connect("clicked", self.on_update_clicked)
        content.append(update_button)
        
        scrolled.set_child(content)
        self.append(scrolled)
    
    def get_repositories(self):
        repos = []
        try:
            with open('/etc/pacman.conf', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('[') and line.endswith(']') and line != '[options]':
                        repo = line[1:-1]
                        repos.append(repo)
        except:
            repos = ['core', 'extra', 'multilib']
        return repos
    
    def on_update_clicked(self, button):
        button.set_sensitive(False)
        button.set_label("Updating...")
        
        def update_repos():
            try:
                subprocess.run(['pkexec', 'pacman', '-Sy'], check=True)
                GLib.idle_add(self.update_complete, button, True)
            except:
                GLib.idle_add(self.update_complete, button, False)
        
        threading.Thread(target=update_repos, daemon=True).start()
    
    def update_complete(self, button, success):
        if success:
            button.set_label("✓ Update completed")
        else:
            button.set_label("✗ Update failed")
        GLib.timeout_add_seconds(3, lambda: button.set_label("Update Package List"))
        GLib.timeout_add_seconds(3, lambda: button.set_sensitive(True))

class PreferencesPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        
        title = Gtk.Label(label="Preferences")
        title.add_css_class("title-1")
        title.set_xalign(0)
        content.append(title)
        
        general_group = Adw.PreferencesGroup()
        general_group.set_title("General")
        general_group.set_margin_top(24)
        
        auto_refresh_row = Adw.ActionRow(title="Auto-refresh package list")
        auto_refresh_row.set_subtitle("Automatically update package list on startup")
        auto_refresh_switch = Gtk.Switch()
        auto_refresh_switch.set_active(True)
        auto_refresh_switch.set_valign(Gtk.Align.CENTER)
        auto_refresh_row.add_suffix(auto_refresh_switch)
        general_group.add(auto_refresh_row)
        
        aur_row = Adw.ActionRow(title="Show AUR packages")
        aur_row.set_subtitle("Include AUR packages in search results")
        aur_switch = Gtk.Switch()
        aur_switch.set_active(True)
        aur_switch.set_valign(Gtk.Align.CENTER)
        aur_row.add_suffix(aur_switch)
        general_group.add(aur_row)
        
        content.append(general_group)
        
        install_group = Adw.PreferencesGroup()
        install_group.set_title("Installation")
        install_group.set_margin_top(24)
        
        details_row = Adw.ActionRow(title="Show installation details")
        details_row.set_subtitle("Show detailed output during installation")
        details_switch = Gtk.Switch()
        details_switch.set_active(False)
        details_switch.set_valign(Gtk.Align.CENTER)
        details_row.add_suffix(details_switch)
        install_group.add(details_row)
        
        confirm_row = Adw.ActionRow(title="Confirm before installation")
        confirm_row.set_subtitle("Show confirmation dialog before installing packages")
        confirm_switch = Gtk.Switch()
        confirm_switch.set_active(True)
        confirm_switch.set_valign(Gtk.Align.CENTER)
        confirm_row.add_suffix(confirm_switch)
        install_group.add(confirm_row)
        
        content.append(install_group)
        
        cache_group = Adw.PreferencesGroup()
        cache_group.set_title("Cache")
        cache_group.set_margin_top(24)
        
        clear_cache_button = Gtk.Button(label="Clear Package Cache")
        clear_cache_button.connect("clicked", self.on_clear_cache_clicked)
        cache_group.add(clear_cache_button)
        
        content.append(cache_group)
        
        scrolled.set_child(content)
        self.append(scrolled)
    
    def on_clear_cache_clicked(self, button):
        dialog = Adw.MessageDialog(
            transient_for=self.get_root(),
            heading="Clear Package Cache",
            body="This will remove all downloaded package files. Are you sure?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear Cache")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self.on_clear_cache_response)
        dialog.present()
    
    def on_clear_cache_response(self, dialog, response):
        if response == "clear":
            button = dialog.get_widget_for_response(response)
            button.set_sensitive(False)
            
            def clear_cache():
                try:
                    subprocess.run(['pkexec', 'pacman', '-Scc', '--noconfirm'], check=True)
                    GLib.idle_add(self.show_toast, "Package cache cleared successfully")
                except:
                    GLib.idle_add(self.show_toast, "Failed to clear package cache")
            
            threading.Thread(target=clear_cache, daemon=True).start()
    
    def show_toast(self, message):
        toast = Adw.Toast(title=message)
        self.get_root().add_toast(toast)

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_default_size(1000, 700)
        self.set_title("Dastore")
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "dastore.png")
        
        if os.path.exists(icon_path):
            try:
                icon_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 64, 64, True)
                self.set_icon(icon_pixbuf)
            except:
                pass
        
        self.toast_overlay = Adw.ToastOverlay()
        self.queue = PackageQueue()
        
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        self.headerbar = Adw.HeaderBar()
        self.main_box.append(self.headerbar)
        
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search packages...")
        self.search_entry.set_size_request(300, -1)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.headerbar.set_title_widget(self.search_entry)
        
        self.filter_button = Gtk.Button(icon_name="funnel-symbolic")
        self.filter_button.set_tooltip_text("Filter packages")
        self.filter_button.connect("clicked", self.on_filter_clicked)
        self.headerbar.pack_start(self.filter_button)
        
        self.queue_button = Gtk.Button(icon_name="document-open-recent-symbolic")
        self.queue_button.set_tooltip_text("Queue")
        self.queue_button.connect("clicked", self.on_queue_clicked)
        self.headerbar.pack_start(self.queue_button)
        
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_button.connect("clicked", self.on_back_clicked)
        self.back_button.set_visible(False)
        self.headerbar.pack_start(self.back_button)
        
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        
        menu = Gio.Menu()
        menu.append("Quick Install", "app.quick_install")
        menu.append("History", "app.history")
        menu.append("Size Sort", "app.size_sort")
        menu.append("Update Repos", "app.update_repos")
        menu.append("Update System", "app.update_system")
        menu.append("Queue", "app.queue")
        menu.append("Preferences", "app.preferences")
        menu.append("About", "app.about")
        menu_button.set_menu_model(menu)
        self.headerbar.pack_end(menu_button)
        
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        
        self.quick_install_page = QuickInstallPage(self)
        self.stack.add_named(self.quick_install_page, "quick_install")
        
        self.history_page = HistoryPage()
        self.stack.add_named(self.history_page, "history")
        
        self.size_sort_page = SizeSortPage()
        self.stack.add_named(self.size_sort_page, "size_sort")
        
        self.queue_page = QueuePage(self.queue)
        self.stack.add_named(self.queue_page, "queue")
        
        self.packages_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        self.filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.filter_bar.set_margin_start(24)
        self.filter_bar.set_margin_end(24)
        self.filter_bar.set_margin_top(12)
        self.filter_bar.set_margin_bottom(6)
        self.filter_bar.set_visible(False)
        
        self.filter_all = Gtk.ToggleButton(label="All")
        self.filter_all.set_active(True)
        self.filter_all.connect("toggled", self.on_filter_toggled, "all")
        self.filter_bar.append(self.filter_all)
        
        self.filter_installed = Gtk.ToggleButton(label="Installed")
        self.filter_installed.connect("toggled", self.on_filter_toggled, "installed")
        self.filter_bar.append(self.filter_installed)
        
        self.filter_not_installed = Gtk.ToggleButton(label="Not Installed")
        self.filter_not_installed.connect("toggled", self.on_filter_toggled, "not_installed")
        self.filter_bar.append(self.filter_not_installed)
        
        self.filter_repo = Gtk.ToggleButton(label="Official Repos")
        self.filter_repo.connect("toggled", self.on_filter_toggled, "repo")
        self.filter_bar.append(self.filter_repo)
        
        self.filter_aur = Gtk.ToggleButton(label="AUR")
        self.filter_aur.connect("toggled", self.on_filter_toggled, "aur")
        self.filter_bar.append(self.filter_aur)
        
        self.packages_page.append(self.filter_bar)
        
        self.status_label = Gtk.Label(label="Searching...")
        self.status_label.set_margin_top(24)
        self.status_label.add_css_class("dim-label")
        self.packages_page.append(self.status_label)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_start(24)
        self.listbox.set_margin_end(24)
        self.listbox.set_margin_top(12)
        self.listbox.set_margin_bottom(24)
        self.listbox.connect("row-activated", self.on_package_selected)
        
        scrolled.set_child(self.listbox)
        self.packages_page.append(scrolled)
        
        self.stack.add_named(self.packages_page, "packages")
        
        self.main_box.append(self.stack)
        
        self.toast_overlay.set_child(self.main_box)
        self.set_content(self.toast_overlay)
        
        self.packages = []
        self.current_detail_page = None
        self.current_filter = "all"
        self.current_page = "quick_install"
        self.last_search_query = ""
        self.search_timer = None
        
        threading.Thread(target=self.check_for_updates, daemon=True).start()
    
    def on_quick_install(self):
        self.stack.set_visible_child_name("quick_install")
        self.back_button.set_visible(False)
        self.current_page = "quick_install"
    
    def on_history(self):
        self.stack.set_visible_child_name("history")
        self.back_button.set_visible(True)
        self.current_page = "history"
    
    def on_size_sort(self):
        self.stack.set_visible_child_name("size_sort")
        self.back_button.set_visible(True)
        self.current_page = "size_sort"
    
    def on_update_repos(self):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Update Repositories",
            body="This will update the package database from all configured repositories."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self.on_update_repos_response)
        dialog.present()
    
    def on_update_repos_response(self, dialog, response):
        if response == "update":
            progress_window = InstallProgressWindow(self, None, False, "update_repos")
            progress_window.present()
    
    def on_update_system(self):
        dialog = UpdateDialog(self, is_system=True)
        dialog.present()
    
    def on_queue_clicked(self, button):
        self.stack.set_visible_child_name("queue")
        self.back_button.set_visible(True)
        self.current_page = "queue"
    
    def on_filter_clicked(self, button):
        self.filter_bar.set_visible(not self.filter_bar.get_visible())
    
    def on_filter_toggled(self, button, filter_type):
        if button.get_active():
            if filter_type == "all":
                self.filter_installed.set_active(False)
                self.filter_not_installed.set_active(False)
                self.filter_repo.set_active(False)
                self.filter_aur.set_active(False)
            elif filter_type == "installed":
                self.filter_all.set_active(False)
                self.filter_not_installed.set_active(False)
                self.filter_repo.set_active(False)
                self.filter_aur.set_active(False)
            elif filter_type == "not_installed":
                self.filter_all.set_active(False)
                self.filter_installed.set_active(False)
                self.filter_repo.set_active(False)
                self.filter_aur.set_active(False)
            elif filter_type == "repo":
                self.filter_all.set_active(False)
                self.filter_installed.set_active(False)
                self.filter_not_installed.set_active(False)
                self.filter_aur.set_active(False)
            elif filter_type == "aur":
                self.filter_all.set_active(False)
                self.filter_installed.set_active(False)
                self.filter_not_installed.set_active(False)
                self.filter_repo.set_active(False)
            
            self.current_filter = filter_type
            self.apply_filter()
    
    def apply_filter(self):
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        
        filtered_packages = []
        
        if self.current_filter == "all":
            filtered_packages = self.packages
        elif self.current_filter == "installed":
            filtered_packages = [p for p in self.packages if p.installed]
        elif self.current_filter == "not_installed":
            filtered_packages = [p for p in self.packages if not p.installed]
        elif self.current_filter == "repo":
            filtered_packages = [p for p in self.packages if p.repo.lower() != "aur"]
        elif self.current_filter == "aur":
            filtered_packages = [p for p in self.packages if p.repo.lower() == "aur"]
        
        if filtered_packages:
            self.status_label.set_visible(False)
            for pkg in filtered_packages:
                row = PackageRow(pkg, self.queue)
                self.listbox.append(row)
        else:
            self.status_label.set_label("No packages found with current filter")
            self.status_label.set_visible(True)
    
    def on_search_changed(self, entry):
        query = entry.get_text().strip()
        
        if self.search_timer:
            GLib.source_remove(self.search_timer)
        
        if len(query) >= 2:
            self.search_timer = GLib.timeout_add(500, self.perform_search, query)
        else:
            self.packages = []
            self.update_package_list([], query)
    
    def perform_search(self, query):
        self.last_search_query = query.lower()
        self.stack.set_visible_child_name("packages")
        self.back_button.set_visible(True)
        self.current_page = "packages"
        self.status_label.set_label(f"Searching for '{query}'...")
        self.status_label.set_visible(True)
        
        async_op.run_async(self.search_packages, query, callback=self.search_complete)
        return False
    
    def search_complete(self, result, error):
        if error:
            self.status_label.set_label(f"Search error: {str(error)}")
            self.status_label.set_visible(True)
        else:
            packages, query = result
            self.update_package_list(packages, query)
    
    def calculate_relevance_score(self, package, query):
        score = 0
        query_lower = query.lower()
        name_lower = package.name.lower()
        desc_lower = package.description.lower() if package.description else ""
        
        if name_lower == query_lower:
            score += 1000
        elif name_lower.startswith(query_lower):
            score += 800
        elif f" {query_lower} " in f" {name_lower} " or name_lower.endswith(query_lower):
            score += 600
        elif query_lower in name_lower:
            score += 400
        elif f" {query_lower} " in f" {desc_lower} ":
            score += 200
        elif query_lower in desc_lower:
            score += 100
        
        if package.installed:
            score += 50
        
        if package.repo.lower() != "aur":
            score += 30
        
        score += max(0, 20 - len(package.name) // 2)
        
        if len(package.name) > 20:
            score -= 20
        
        package.relevance_score = score
        return score
    
    def search_packages(self, query):
        packages = []
        
        try:
            result = subprocess.run(['pacman', '-Ss', query], 
                                  capture_output=True, text=True, timeout=10)
            packages.extend(self.parse_pacman_search(result.stdout))
        except:
            pass
        
        try:
            result = subprocess.run(['yay', '-Ss', '--aur', query], 
                                  capture_output=True, text=True, timeout=10)
            packages.extend(self.parse_aur_search(result.stdout))
        except:
            pass
        
        seen_packages = {}
        unique_packages = []
        for package in packages:
            if package.name not in seen_packages:
                seen_packages[package.name] = package
                unique_packages.append(package)
            elif package.repo.lower() != "aur" and seen_packages[package.name].repo.lower() == "aur":
                idx = unique_packages.index(seen_packages[package.name])
                unique_packages[idx] = package
                seen_packages[package.name] = package
        
        for package in unique_packages:
            self.calculate_relevance_score(package, query)
        
        unique_packages.sort(key=lambda p: p.relevance_score, reverse=True)
        
        return unique_packages, query
    
    def check_for_updates(self):
        try:
            result = subprocess.run(['checkupdates'], 
                                  capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0 and result.stdout:
                updates = result.stdout.strip().split('\n')
                for update in updates:
                    parts = update.split()
                    if len(parts) >= 2:
                        name = parts[0]
        except:
            pass
    
    def parse_pacman_search(self, output):
        packages = []
        lines = output.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            if line and not line.startswith(' '):
                parts = line.split('/')
                if len(parts) >= 2:
                    repo = parts[0]
                    name_version = parts[1].split()
                    if len(name_version) >= 2:
                        name = name_version[0]
                        version = name_version[1]
                        
                        installed = '[installed]' in line or '[installed:' in line
                        
                        description = ""
                        if i + 1 < len(lines) and lines[i + 1].startswith('    '):
                            description = lines[i + 1].strip()
                        
                        pkg = PackageInfo(
                            name=name,
                            version=version,
                            description=description,
                            repo=repo,
                            installed=installed
                        )
                        packages.append(pkg)
            i += 1
        
        return packages
    
    def parse_aur_search(self, output):
        packages = []
        lines = output.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('aur/'):
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0].replace('aur/', '')
                    version = parts[1]
                    
                    installed = '[installed]' in line or '[installed:' in line
                    
                    description = ""
                    if i + 1 < len(lines) and lines[i + 1].startswith('    '):
                        description = lines[i + 1].strip()
                    
                    pkg = PackageInfo(
                        name=name,
                        version=version,
                        description=description,
                        repo="AUR",
                        installed=installed
                    )
                    packages.append(pkg)
            i += 1
        
        return packages
    
    def update_package_list(self, packages, query):
        self.packages = packages
        
        if packages:
            self.status_label.set_visible(False)
            self.apply_filter()
        else:
            self.status_label.set_label(f"No packages found for '{query}'")
            self.status_label.set_visible(True)
    
    def on_package_selected(self, listbox, row):
        package = row.package
        
        async_op.run_async(self.get_package_details, package, callback=self.package_details_complete)
    
    def package_details_complete(self, result, error):
        if error:
            self.show_package_details(result)
        else:
            self.show_package_details(result)
    
    def get_package_details(self, package):
        is_aur = package.repo.lower() == "aur"
        
        try:
            if is_aur:
                result = subprocess.run(['yay', '-Si', package.name], 
                                      capture_output=True, text=True, timeout=10)
            else:
                result = subprocess.run(['pacman', '-Si', package.name], 
                                      capture_output=True, text=True, timeout=10)
            
            details = self.parse_package_info(result.stdout, package)
            return details
        except:
            return package
    
    def parse_package_info(self, output, package):
        details = PackageInfo(
            name=package.name,
            version=package.version,
            description=package.description,
            repo=package.repo,
            installed=package.installed
        )
        
        for line in output.split('\n'):
            line = line.strip()
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
                
                if key == "Description":
                    details.description = value
                elif key == "URL":
                    details.url = value
                elif key == "Licenses":
                    details.licenses = value
                elif key == "Download Size":
                    details.size = value
                elif key == "Installed Size":
                    details.installed_size = value
                elif key == "Depends On":
                    details.depends = value
                elif key == "Groups":
                    details.groups = value
        
        return details
    
    def show_package_details(self, package):
        if self.current_detail_page:
            self.stack.remove(self.current_detail_page)
        
        self.current_detail_page = PackageDetailPage(package, self)
        self.stack.add_named(self.current_detail_page, "detail")
        self.stack.set_visible_child_name("detail")
        
        self.back_button.set_visible(True)
        self.current_page = "detail"
    
    def on_back_clicked(self, button):
        if self.current_page == "detail":
            self.stack.set_visible_child_name("packages")
            self.current_page = "packages"
        elif self.current_page == "packages":
            self.stack.set_visible_child_name("quick_install")
            self.back_button.set_visible(False)
            self.current_page = "quick_install"
        elif self.current_page in ["queue", "history", "size_sort", "repos", "preferences"]:
            self.stack.set_visible_child_name("quick_install")
            self.back_button.set_visible(False)
            self.current_page = "quick_install"
    
    def show_repositories(self):
        if self.current_detail_page:
            self.stack.remove(self.current_detail_page)
        
        self.current_detail_page = RepositoriesPage()
        self.stack.add_named(self.current_detail_page, "repos")
        self.stack.set_visible_child_name("repos")
        self.back_button.set_visible(True)
        self.current_page = "repos"
    
    def show_preferences(self):
        if self.current_detail_page:
            self.stack.remove(self.current_detail_page)
        
        self.current_detail_page = PreferencesPage()
        self.stack.add_named(self.current_detail_page, "preferences")
        self.stack.set_visible_child_name("preferences")
        self.back_button.set_visible(True)
        self.current_page = "preferences"
    
    def show_install_progress(self, package, is_aur):
        progress_window = InstallProgressWindow(self, package, is_aur, "install")
        progress_window.present()
    
    def show_uninstall_progress(self, package):
        progress_window = InstallProgressWindow(self, package, False, "uninstall")
        progress_window.present()
    
    def show_update_progress(self, package):
        is_aur = package.repo.lower() == "aur"
        progress_window = InstallProgressWindow(self, package, is_aur, "update")
        progress_window.present()
    
    def show_system_update_progress(self):
        progress_window = InstallProgressWindow(self, None, False, "system_update")
        progress_window.present()
    
    def show_queue_install_progress(self):
        progress_window = InstallProgressWindow(self, None, False, "queue_install", self.queue)
        progress_window.present()
    
    def add_toast(self, message):
        toast = Adw.Toast(title=message)
        self.toast_overlay.add_toast(toast)

class DastoreApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.daradege.dastore")
        
        style_manager = Adw.StyleManager.get_default()
        style_manager.set_color_scheme(Adw.ColorScheme.PREFER_LIGHT)
        
    def do_activate(self):
        win = MainWindow(self)
        
        quick_install_action = Gio.SimpleAction.new("quick_install", None)
        quick_install_action.connect("activate", lambda a, p: win.on_quick_install())
        self.add_action(quick_install_action)
        
        history_action = Gio.SimpleAction.new("history", None)
        history_action.connect("activate", lambda a, p: win.on_history())
        self.add_action(history_action)
        
        size_sort_action = Gio.SimpleAction.new("size_sort", None)
        size_sort_action.connect("activate", lambda a, p: win.on_size_sort())
        self.add_action(size_sort_action)
        
        update_repos_action = Gio.SimpleAction.new("update_repos", None)
        update_repos_action.connect("activate", lambda a, p: win.on_update_repos())
        self.add_action(update_repos_action)
        
        update_system_action = Gio.SimpleAction.new("update_system", None)
        update_system_action.connect("activate", lambda a, p: win.on_update_system())
        self.add_action(update_system_action)
        
        queue_action = Gio.SimpleAction.new("queue", None)
        queue_action.connect("activate", lambda a, p: win.on_queue_clicked(None))
        self.add_action(queue_action)
        
        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", lambda a, p: win.show_preferences())
        self.add_action(prefs_action)
        
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.show_about)
        self.add_action(about_action)
        
        win.present()
    
    def show_about(self, action, param):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "dastore.png")
        
        about = Adw.AboutWindow(
            transient_for=self.get_active_window(),
            application_name="Dastore",
            developer_name="Ali Safamanesh",
            version="1.0.0",
            website="https://daradege.ir",
            issue_url="https://github.com/daradege",
            license_type=Gtk.License.GPL_3_0
        )
        
        if os.path.exists(icon_path):
            try:
                icon_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 128, 128, True)
                about.set_logo(icon_pixbuf)
            except:
                about.set_icon_name("package-x-generic")
        else:
            about.set_icon_name("package-x-generic")
        
        about.present()
    
    def do_shutdown(self):
        async_op.shutdown()
        super().do_shutdown()

if __name__ == "__main__":
    app = DastoreApp()
    app.run(None)
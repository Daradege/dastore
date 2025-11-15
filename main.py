import gi
import subprocess
import json
import os
import re
import time
import requests
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, List, Callable
from enum import Enum

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

@dataclass
class PackageInfo:
    name: str = ""
    version: str = ""
    description: str = ""
    repo: str = ""
    size: str = ""
    installed_size: str = ""
    depends: str = ""
    url: str = ""
    licenses: str = ""
    groups: str = ""
    installed: bool = False
    update_available: bool = False
    relevance_score: int = 0

class OperationType(Enum):
    INSTALL = "install"
    UNINSTALL = "uninstall"
    UPDATE = "update"
    SYSTEM_UPDATE = "system_update"
    QUEUE_INSTALL = "queue_install"

class AsyncManager:
    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._active_tasks = []
    
    def run_async(self, func: Callable, *args, callback: Optional[Callable] = None, **kwargs):
        def wrapper():
            try:
                result = func(*args, **kwargs)
                if callback:
                    GLib.idle_add(callback, result, None)
            except Exception as e:
                if callback:
                    GLib.idle_add(callback, None, e)
        
        future = self.executor.submit(wrapper)
        self._active_tasks.append(future)
        return future
    
    def shutdown(self):
        self.executor.shutdown(wait=False)

async_manager = AsyncManager()

class PackageQueue:
    def __init__(self):
        self._packages: List[PackageInfo] = []
        self._callbacks: List[Callable] = []
    
    def add_package(self, package: PackageInfo):
        if not any(p.name == package.name for p in self._packages):
            self._packages.append(package)
            self._notify_callbacks()
    
    def remove_package(self, package: PackageInfo):
        self._packages = [p for p in self._packages if p.name != package.name]
        self._notify_callbacks()
    
    def clear(self):
        self._packages.clear()
        self._notify_callbacks()
    
    def add_callback(self, callback: Callable):
        self._callbacks.append(callback)
    
    def _notify_callbacks(self):
        for callback in self._callbacks:
            GLib.idle_add(callback)
    
    @property
    def packages(self) -> List[PackageInfo]:
        return self._packages.copy()
    
    def __len__(self) -> int:
        return len(self._packages)

class IconManager:
    ICON_SIZE = 32
    CATEGORY_ICONS = {
        "firefox": "web-browser",
        "chromium": "web-browser",
        "vlc": "multimedia-player",
        "gimp": "image-x-generic",
        "code": "text-editor",
        "git": "terminal",
        "docker": "container",
        "steam": "applications-games",
    }
    
    @classmethod
    def get_icon(cls, package_name: str) -> Gtk.Image:
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        
        if icon_theme.has_icon(package_name):
            return Gtk.Image.new_from_icon_name(package_name)
        
        variations = cls._get_name_variations(package_name)
        for var in variations:
            if icon_theme.has_icon(var):
                return Gtk.Image.new_from_icon_name(var)
        
        icon_image = cls._check_icon_files(package_name, variations)
        if icon_image:
            return icon_image
        
        icon_image = cls._check_desktop_files(package_name, icon_theme)
        if icon_image:
            return icon_image
        
        for key, icon in cls.CATEGORY_ICONS.items():
            if key in package_name.lower():
                return Gtk.Image.new_from_icon_name(icon)
        
        return Gtk.Image.new_from_icon_name("package-x-generic")
    
    @staticmethod
    def _get_name_variations(name: str) -> List[str]:
        return [
            name,
            name.replace('-', '_'),
            name.lower(),
            f"org.{name}",
            f"com.{name}",
            f"io.{name}",
        ]
    
    @staticmethod
    def _check_icon_files(package_name: str, variations: List[str]) -> Optional[Gtk.Image]:
        icon_paths = [
            f"/usr/share/icons/hicolor/64x64/apps/{package_name}.png",
            f"/usr/share/pixmaps/{package_name}.png",
        ]
        
        for var in variations:
            for path_template in icon_paths:
                path = path_template.replace(package_name, var)
                if os.path.exists(path):
                    try:
                        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                            path, IconManager.ICON_SIZE, IconManager.ICON_SIZE, True
                        )
                        return Gtk.Image.new_from_pixbuf(pixbuf)
                    except:
                        pass
        return None
    
    @staticmethod
    def _check_desktop_files(package_name: str, icon_theme) -> Optional[Gtk.Image]:
        desktop_paths = [
            f"/usr/share/applications/{package_name}.desktop",
            f"/usr/share/applications/org.{package_name}.desktop",
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
                except:
                    pass
        return None

class PackageManager:
    @staticmethod
    def search_packages(query: str) -> List[PackageInfo]:
        try:
            result = subprocess.run(
                ['pacman', '-Ss', query],
                capture_output=True,
                text=True,
                timeout=10
            )
            packages = PackageManager._parse_search_output(result.stdout)
            
            for pkg in packages:
                PackageManager._calculate_relevance(pkg, query)
            
            packages.sort(key=lambda p: p.relevance_score, reverse=True)
            return packages
        except Exception as e:
            print(f"Search error: {e}")
            return []
    
    @staticmethod
    def get_package_details(package: PackageInfo) -> PackageInfo:
        try:
            result = subprocess.run(
                ['pacman', '-Si', package.name],
                capture_output=True,
                text=True,
                timeout=10
            )
            return PackageManager._parse_info_output(result.stdout, package)
        except:
            return package
    
    @staticmethod
    def _parse_search_output(output: str) -> List[PackageInfo]:
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
                        installed = '[installed]' in line
                        
                        description = ""
                        if i + 1 < len(lines) and lines[i + 1].startswith('    '):
                            description = lines[i + 1].strip()
                        
                        packages.append(PackageInfo(
                            name=name,
                            version=version,
                            description=description,
                            repo=repo,
                            installed=installed
                        ))
            i += 1
        
        return packages
    
    @staticmethod
    def _parse_info_output(output: str, package: PackageInfo) -> PackageInfo:
        details = PackageInfo(
            name=package.name,
            version=package.version,
            description=package.description,
            repo=package.repo,
            installed=package.installed
        )
        
        for line in output.split('\n'):
            if ':' not in line:
                continue
            
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
        
        return details
    
    @staticmethod
    def _calculate_relevance(package: PackageInfo, query: str):
        score = 0
        query_lower = query.lower()
        name_lower = package.name.lower()
        
        if name_lower == query_lower:
            score += 1000
        elif name_lower.startswith(query_lower):
            score += 800
        elif query_lower in name_lower:
            score += 400
        
        if package.installed:
            score += 50
        
        package.relevance_score = score

class PackageRow(Gtk.ListBoxRow):
    def __init__(self, package: PackageInfo, queue: Optional[PackageQueue] = None):
        super().__init__()
        self.package = package
        self.queue = queue
        
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        
        icon = IconManager.get_icon(package.name)
        icon.set_pixel_size(32)
        box.append(icon)
        
        info_box = self._create_info_box()
        box.append(info_box)
        
        if queue and not package.installed:
            queue_button = Gtk.Button(icon_name="list-add-symbolic")
            queue_button.set_tooltip_text("Add to queue")
            queue_button.add_css_class("flat")
            queue_button.connect("clicked", self._on_queue_clicked)
            box.append(queue_button)
        
        self.set_child(box)
    
    def _create_info_box(self) -> Gtk.Box:
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_hexpand(True)
        
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label=self.package.name)
        name_label.set_xalign(0)
        name_label.add_css_class("heading")
        name_box.append(name_label)
        
        if self.package.installed:
            badge = Gtk.Label(label="Installed")
            badge.add_css_class("success")
            badge.add_css_class("caption")
            name_box.append(badge)
        
        info_box.append(name_box)
        
        desc = self.package.description
        if len(desc) > 80:
            desc = desc[:80] + "..."
        
        desc_label = Gtk.Label(label=desc)
        desc_label.set_xalign(0)
        desc_label.add_css_class("dim-label")
        desc_label.add_css_class("caption")
        info_box.append(desc_label)
        
        version_label = Gtk.Label(label=f"{self.package.version} • {self.package.repo}")
        version_label.set_xalign(0)
        version_label.add_css_class("caption")
        info_box.append(version_label)
        
        return info_box
    
    def _on_queue_clicked(self, button):
        if self.queue:
            self.queue.add_package(self.package)

class ProgressWindow(Adw.Window):
    def __init__(self, parent, operation: OperationType, package: Optional[PackageInfo] = None, queue: Optional[PackageQueue] = None):
        super().__init__(transient_for=parent)
        self.set_default_size(700, 500)
        
        self.operation = operation
        self.package = package
        self.queue = queue
        self.process = None
        self.cancelled = False
        self.completed = False
        
        self._setup_ui()
        self._start_operation()
    
    def _setup_ui(self):
        title_map = {
            OperationType.INSTALL: f"Installing {self.package.name}" if self.package else "Installing",
            OperationType.UNINSTALL: f"Uninstalling {self.package.name}",
            OperationType.UPDATE: f"Updating {self.package.name}",
            OperationType.SYSTEM_UPDATE: "System Update",
            OperationType.QUEUE_INSTALL: "Installing Queue",
        }
        self.set_title(title_map.get(self.operation, "Operation"))
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        headerbar = Adw.HeaderBar()
        content.append(headerbar)
        
        progress_box = self._create_progress_box()
        content.append(progress_box)
        
        self.expander = self._create_log_expander()
        content.append(self.expander)
        
        button_box = self._create_button_box()
        content.append(button_box)
        
        self.set_content(content)
    
    def _create_progress_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_text("0%")
        box.append(self.progress_bar)
        
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.status_label = Gtk.Label(label="Preparing...")
        self.status_label.set_hexpand(True)
        self.status_label.set_xalign(0)
        status_box.append(self.status_label)
        box.append(status_box)
        
        return box
    
    def _create_log_expander(self) -> Gtk.Expander:
        expander = Gtk.Expander(label="Show details")
        expander.set_margin_start(12)
        expander.set_margin_end(12)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(200)
        
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_margin_start(12)
        self.textview.set_margin_end(12)
        
        self.buffer = self.textview.get_buffer()
        scrolled.set_child(self.textview)
        expander.set_child(scrolled)
        
        return expander
    
    def _create_button_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_bottom(12)
        box.set_halign(Gtk.Align.END)
        
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._on_cancel)
        box.append(self.cancel_button)
        
        self.close_button = Gtk.Button(label="Close")
        self.close_button.connect("clicked", lambda b: self.close())
        self.close_button.set_visible(False)
        box.append(self.close_button)
        
        return box
    
    def _start_operation(self):
        async_manager.run_async(self._execute_operation)
    
    def _execute_operation(self):
        try:
            self._update_progress(0.05, "Starting...")
            
            cmd = self._build_command()
            self._append_log(f"Running: {' '.join(cmd)}\n\n")
            
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
            except:
                pass
            
            self._process_output()
            
            if not self.cancelled:
                self.process.wait()
                
                if self.process.returncode == 0:
                    self.completed = True
                    self._update_progress(1.0, "✓ Completed successfully!")
                    self._append_log("\n✓ Operation completed successfully!\n")
                    
                    if self.queue:
                        self.queue.clear()
                else:
                    self._update_progress(1.0, "✗ Operation failed")
                    self._append_log("\n✗ Operation failed\n")
        
        except Exception as e:
            self._update_progress(1.0, f"✗ Error: {str(e)}")
            self._append_log(f"\n✗ Error: {str(e)}\n")
        
        finally:
            GLib.idle_add(self._operation_complete)
    
    def _build_command(self) -> List[str]:
        base = ['pkexec', 'pacman']
        
        if self.operation == OperationType.INSTALL:
            return base + ['-S', '--noconfirm', self.package.name]
        elif self.operation == OperationType.UNINSTALL:
            return base + ['-R', '--noconfirm', self.package.name]
        elif self.operation == OperationType.UPDATE:
            return base + ['-S', '--noconfirm', self.package.name]
        elif self.operation == OperationType.SYSTEM_UPDATE:
            return base + ['-Syu', '--noconfirm']
        elif self.operation == OperationType.QUEUE_INSTALL:
            names = [p.name for p in self.queue.packages]
            return base + ['-S', '--noconfirm'] + names
        
        return base
    
    def _process_output(self):
        progress_pattern = re.compile(r'\((\d+)%\)')
        
        while True:
            if self.cancelled:
                break
            
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
            except:
                break
            
            self._append_log(line)
            
            match = progress_pattern.search(line)
            if match:
                percent = int(match.group(1)) / 100.0
                if "downloading" in line.lower():
                    self._update_progress(0.1 + percent * 0.3, "Downloading...")
                elif "installing" in line.lower():
                    self._update_progress(0.4 + percent * 0.5, "Installing...")
            
            if ":: proceed" in line.lower():
                try:
                    self.process.stdin.write("Y\n")
                    self.process.stdin.flush()
                except:
                    pass
    
    def _update_progress(self, fraction: float, status: str):
        GLib.idle_add(self._update_progress_ui, fraction, status)
    
    def _update_progress_ui(self, fraction: float, status: str):
        self.progress_bar.set_fraction(fraction)
        self.progress_bar.set_text(f"{int(fraction * 100)}%")
        self.status_label.set_label(status)
    
    def _append_log(self, text: str):
        GLib.idle_add(self._append_log_ui, text)
    
    def _append_log_ui(self, text: str):
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text)
        
        mark = self.buffer.create_mark(None, end_iter, False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
    
    def _on_cancel(self, button):
        self.cancelled = True
        if self.process:
            try:
                self.process.terminate()
            except:
                pass
        
        self.cancel_button.set_sensitive(False)
        self._update_progress(1.0, "Cancelled")
    
    def _operation_complete(self):
        self.cancel_button.set_visible(False)
        self.close_button.set_visible(True)

class HomePage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        
        self._setup_ui()
    
    def _setup_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        main_box.set_size_request(600, -1)
        
        title = Gtk.Label(label="Dastore")
        title.add_css_class("title-1")
        main_box.append(title)
        
        subtitle = Gtk.Label(label="Package Manager for Arch Linux")
        subtitle.add_css_class("title-3")
        subtitle.add_css_class("dim-label")
        main_box.append(subtitle)
        
        features_box = self._create_features_box()
        main_box.append(features_box)
        
        quick_actions = self._create_quick_actions()
        main_box.append(quick_actions)
        
        self.append(main_box)
    
    def _create_features_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        
        features = [
            ("🔍 Search Packages", "Find and install packages from official repositories"),
            ("⚡ Quick Install", "Install packages with a single click"),
            ("📦 Queue Management", "Queue multiple packages for batch installation"),
            ("🔄 System Update", "Update your system with one click"),
            ("🎯 Smart Search", "Relevant results with intelligent scoring")
        ]
        
        for icon_text, description in features:
            feature_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            icon = Gtk.Label(label=icon_text)
            icon.add_css_class("heading")
            
            desc_label = Gtk.Label(label=description)
            desc_label.set_xalign(0)
            desc_label.add_css_class("dim-label")
            
            feature_box.append(icon)
            feature_box.append(desc_label)
            box.append(feature_box)
        
        return box
    
    def _create_quick_actions(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        
        return box
    
    
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_default_size(1000, 700)
        self.set_title("Dastore")
        
        self.queue = PackageQueue()
        self.packages = []
        self.search_timer = None
        
        self._setup_ui()
        self._load_icon()
    
    def _load_icon(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "dastore.png")
        
        if os.path.exists(icon_path):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 64, 64, True)
                self.set_icon(pixbuf)
            except:
                pass
    
    def _setup_ui(self):
        self.toast_overlay = Adw.ToastOverlay()
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        headerbar = self._create_headerbar()
        main_box.append(headerbar)
        
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        
        self.home_page = HomePage()
        self.stack.add_named(self.home_page, "home")
        
        self.packages_page = self._create_packages_page()
        self.stack.add_named(self.packages_page, "packages")
        
        main_box.append(self.stack)
        
        self.toast_overlay.set_child(main_box)
        self.set_content(self.toast_overlay)
        
        self.stack.set_visible_child_name("home")
    
    def _create_headerbar(self) -> Adw.HeaderBar:
        headerbar = Adw.HeaderBar()
        
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search packages...")
        self.search_entry.set_size_request(300, -1)
        self.search_entry.connect("search-changed", self._on_search_changed)
        headerbar.set_title_widget(self.search_entry)
        
        queue_button = Gtk.Button(icon_name="document-open-recent-symbolic")
        queue_button.set_tooltip_text("Queue")
        queue_button.connect("clicked", self._show_queue)
        headerbar.pack_start(queue_button)
        
        menu_button = self._create_menu_button()
        headerbar.pack_end(menu_button)
        
        return headerbar
    
    def _create_menu_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        
        menu = Gio.Menu()
        menu.append("Update System", "app.update_system")
        menu.append("About", "app.about")
        
        button.set_menu_model(menu)
        return button
    
    def _create_packages_page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        self.status_label = Gtk.Label(label="Search for packages...")
        self.status_label.set_margin_top(24)
        self.status_label.add_css_class("dim-label")
        page.append(self.status_label)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_start(24)
        self.listbox.set_margin_end(24)
        self.listbox.set_margin_top(12)
        self.listbox.set_margin_bottom(24)
        self.listbox.connect("row-activated", self._on_package_selected)
        
        scrolled.set_child(self.listbox)
        page.append(scrolled)
        
        return page
    
    def _on_search_changed(self, entry):
        query = entry.get_text().strip()
        
        if self.search_timer:
            GLib.source_remove(self.search_timer)
        
        if len(query) >= 2:
            self.search_timer = GLib.timeout_add(500, self._perform_search, query)
        else:
            self._clear_packages()
    
    def _perform_search(self, query: str):
        self.stack.set_visible_child_name("packages")
        self.status_label.set_label(f"Searching for '{query}'...")
        self.status_label.set_visible(True)
        
        async_manager.run_async(
            PackageManager.search_packages,
            query,
            callback=self._search_complete
        )
        
        return False
    
    def _search_complete(self, packages, error):
        if error:
            self.status_label.set_label(f"Search error: {str(error)}")
        elif packages:
            self.packages = packages
            self._update_package_list()
        else:
            self.status_label.set_label("No packages found")
    
    def _update_package_list(self):
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        
        if self.packages:
            self.status_label.set_visible(False)
            for pkg in self.packages[:50]:
                row = PackageRow(pkg, self.queue)
                self.listbox.append(row)
        else:
            self.status_label.set_visible(True)
    
    def _clear_packages(self):
        self.packages = []
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        
        self.stack.set_visible_child_name("home")
    
    def _on_package_selected(self, listbox, row):
        package = row.package
        
        async_manager.run_async(
            PackageManager.get_package_details,
            package,
            callback=self._show_package_details
        )
    
    def _show_package_details(self, package, error):
        if error:
            self._show_toast(f"Error: {str(error)}")
            return
        
        dialog = PackageDetailDialog(self, package, self.queue)
        dialog.present()
    
    def _show_queue(self, button):
        dialog = QueueDialog(self, self.queue)
        dialog.present()
    
    def _show_toast(self, message: str):
        toast = Adw.Toast(title=message)
        self.toast_overlay.add_toast(toast)
    
    def update_system(self):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Update System",
            body="Do you want to update all packages?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_update_response)
        dialog.present()
    
    def _on_update_response(self, dialog, response):
        if response == "update":
            progress = ProgressWindow(self, OperationType.SYSTEM_UPDATE)
            progress.present()

class PackageDetailDialog(Adw.Window):
    def __init__(self, parent, package: PackageInfo, queue: PackageQueue):
        super().__init__(transient_for=parent)
        self.set_default_size(600, 500)
        self.set_title(package.name)
        
        self.package = package
        self.queue = queue
        
        self._setup_ui()
    
    def _setup_ui(self):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        headerbar = Adw.HeaderBar()
        content.append(headerbar)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        inner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner_box.set_margin_start(24)
        inner_box.set_margin_end(24)
        inner_box.set_margin_top(24)
        inner_box.set_margin_bottom(24)
        
        name_label = Gtk.Label(label=self.package.name)
        name_label.add_css_class("title-1")
        name_label.set_xalign(0)
        inner_box.append(name_label)
        
        version_label = Gtk.Label(label=f"Version: {self.package.version}")
        version_label.add_css_class("title-4")
        version_label.add_css_class("dim-label")
        version_label.set_xalign(0)
        inner_box.append(version_label)
        
        if self.package.description:
            desc_label = Gtk.Label(label=self.package.description)
            desc_label.set_wrap(True)
            desc_label.set_xalign(0)
            desc_label.set_margin_top(12)
            inner_box.append(desc_label)
        
        info_group = self._create_info_group()
        inner_box.append(info_group)
        
        button_box = self._create_button_box()
        inner_box.append(button_box)
        
        scrolled.set_child(inner_box)
        content.append(scrolled)
        
        self.set_content(content)
    
    def _create_info_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Information")
        group.set_margin_top(24)
        
        if self.package.repo:
            row = Adw.ActionRow(title="Repository")
            row.add_suffix(Gtk.Label(label=self.package.repo))
            group.add(row)
        
        if self.package.size:
            row = Adw.ActionRow(title="Download Size")
            row.add_suffix(Gtk.Label(label=self.package.size))
            group.add(row)
        
        if self.package.installed_size:
            row = Adw.ActionRow(title="Installed Size")
            row.add_suffix(Gtk.Label(label=self.package.installed_size))
            group.add(row)
        
        if self.package.licenses:
            row = Adw.ActionRow(title="License")
            row.add_suffix(Gtk.Label(label=self.package.licenses))
            group.add(row)
        
        return group
    
    def _create_button_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(24)
        box.set_halign(Gtk.Align.CENTER)
        
        if not self.package.installed:
            install_button = Gtk.Button(label="Install")
            install_button.add_css_class("suggested-action")
            install_button.add_css_class("pill")
            install_button.connect("clicked", self._on_install)
            box.append(install_button)
            
            queue_button = Gtk.Button(label="Add to Queue")
            queue_button.add_css_class("pill")
            queue_button.connect("clicked", self._on_add_queue)
            box.append(queue_button)
        else:
            uninstall_button = Gtk.Button(label="Uninstall")
            uninstall_button.add_css_class("destructive-action")
            uninstall_button.add_css_class("pill")
            uninstall_button.connect("clicked", self._on_uninstall)
            box.append(uninstall_button)
        
        return box
    
    def _on_install(self, button):
        progress = ProgressWindow(
            self.get_transient_for(),
            OperationType.INSTALL,
            self.package
        )
        progress.present()
        self.close()
    
    def _on_uninstall(self, button):
        progress = ProgressWindow(
            self.get_transient_for(),
            OperationType.UNINSTALL,
            self.package
        )
        progress.present()
        self.close()
    
    def _on_add_queue(self, button):
        self.queue.add_package(self.package)
        
        parent = self.get_transient_for()
        if hasattr(parent, '_show_toast'):
            parent._show_toast(f"Added {self.package.name} to queue")
        
        self.close()

class QueueDialog(Adw.Window):
    def __init__(self, parent, queue: PackageQueue):
        super().__init__(transient_for=parent)
        self.set_default_size(600, 500)
        self.set_title("Install Queue")
        
        self.queue = queue
        
        self._setup_ui()
        self.queue.add_callback(self._update_list)
    
    def _setup_ui(self):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        headerbar = Adw.HeaderBar()
        content.append(headerbar)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_start(24)
        self.listbox.set_margin_end(24)
        self.listbox.set_margin_top(24)
        self.listbox.connect("row-activated", self._on_row_activated)
        
        scrolled.set_child(self.listbox)
        content.append(scrolled)
        
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(24)
        button_box.set_margin_end(24)
        button_box.set_margin_top(12)
        button_box.set_margin_bottom(24)
        button_box.set_halign(Gtk.Align.CENTER)
        
        self.install_button = Gtk.Button(label="Install All")
        self.install_button.add_css_class("suggested-action")
        self.install_button.add_css_class("pill")
        self.install_button.connect("clicked", self._on_install_all)
        button_box.append(self.install_button)
        
        clear_button = Gtk.Button(label="Clear Queue")
        clear_button.add_css_class("pill")
        clear_button.connect("clicked", lambda b: self.queue.clear())
        button_box.append(clear_button)
        
        content.append(button_box)
        
        self.set_content(content)
        
        self._update_list()
    
    def _update_list(self):
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        
        for package in self.queue.packages:
            row = PackageRow(package)
            self.listbox.append(row)
        
        self.install_button.set_sensitive(len(self.queue) > 0)
    
    def _on_row_activated(self, listbox, row):
        self.queue.remove_package(row.package)
    
    def _on_install_all(self, button):
        if len(self.queue) > 0:
            progress = ProgressWindow(
                self.get_transient_for(),
                OperationType.QUEUE_INSTALL,
                queue=self.queue
            )
            progress.present()
            self.close()

class DastoreApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.daradege.dastore")
        
        style_manager = Adw.StyleManager.get_default()
        style_manager.set_color_scheme(Adw.ColorScheme.PREFER_LIGHT)
    
    def do_activate(self):
        win = MainWindow(self)
        
        self._setup_actions(win)
        
        win.present()
    
    def _setup_actions(self, window: MainWindow):
        update_action = Gio.SimpleAction.new("update_system", None)
        update_action.connect("activate", lambda a, p: window.update_system())
        self.add_action(update_action)
        
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)
    
    def _show_about(self, action, param):
        about = Adw.AboutWindow(
            transient_for=self.get_active_window(),
            application_name="Dastore",
            developer_name="Ali Safamanesh",
            version="2.0.0",
            website="https://daradege.ir",
            license_type=Gtk.License.GPL_3_0
        )
        about.present()
    
    def do_shutdown(self):
        async_manager.shutdown()
        super().do_shutdown()

if __name__ == "__main__":
    app = DastoreApp()
    app.run(None)
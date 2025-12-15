"""
FTP Browser Plugin for InkyPi

Provides:
- FTP server connection
- Directory browsing (recursive)
- Image selection/preview
- Random image from folder selection

Config schema (passed via 'options'):
  server: str (required)           — FTP server hostname or IP
  port: int (optional, default 21) — FTP server port
  username: str (optional, default "anonymous")
  password: str (optional, default "")
  use_tls: bool (optional, default False)
  passive: bool (optional, default True)
  initial_path: str (optional, default "/")  — Initial directory when connecting
  current_path: str (optional, default "/")
  random_mode: bool (optional, default False)
  selected_image: str (optional)   — Path to a specific image to display
"""

import os
import random
import tempfile
import logging
from ftplib import FTP, FTP_TLS, error_perm
from PIL import Image, ImageOps
from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

class FTPBrowser(BasePlugin):
    def __init__(self, config, **dependencies):
        super().__init__(config, **dependencies)
        self.ftp = None
        self.temp_files = []

    def __del__(self):
        self._cleanup_temp_files()
        self._close_ftp()
    
    def _cleanup_temp_files(self):
        """Clean up any temporary files created by this instance"""
        for temp_file in self.temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception as e:
                logger.error(f"Error cleaning up temporary file {temp_file}: {e}")
        self.temp_files = []
    
    def _close_ftp(self):
        """Close the FTP connection if it exists"""
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception as e:
                logger.error(f"Error closing FTP connection: {e}")
            finally:
                self.ftp = None

    def _connect_ftp(self, server, port=21, username="anonymous", password="", use_tls=False, passive=True):
        """Establish FTP/FTPS connection and login."""
        try:
            self._close_ftp()  # Close existing connection if any
            
            ftp_class = FTP_TLS if use_tls else FTP
            self.ftp = ftp_class()
            self.ftp.connect(server, port)
            self.ftp.login(user=username, passwd=password)
            self.ftp.set_pasv(passive)
            
            # Set encoding to latin-1 to handle non-UTF-8 filenames (e.g., with umlauts)
            self.ftp.encoding = 'latin-1'
            
            return self.ftp
        except Exception as e:
            logger.error(f"FTP connection error: {e}")
            raise RuntimeError(f"Failed to connect to FTP server: {e}")

    def _list_directory(self, path="/"):
        """
        List contents of the specified directory.
        Returns dictionary with directories and files separated.
        """
        if not self.ftp:
            raise RuntimeError("FTP connection is not established")
        
        try:
            self.ftp.cwd(path)
            
            # Try to use MLSD command for more detailed listing
            try:
                entries = list(self.ftp.mlsd())
                
                # Separate directories and files
                directories = []
                files = []
                image_exts = (".jpg", ".jpeg", ".png", ".bmp", ".gif")
                
                for name, facts in entries:
                    if name in (".", ".."):
                        continue
                        
                    entry_type = facts.get("type", "")
                    if entry_type == "dir":
                        directories.append({
                            "name": name,
                            "path": os.path.join(path, name).replace("\\", "/"),
                            "type": "dir"
                        })
                    elif entry_type == "file" and name.lower().endswith(image_exts):
                        files.append({
                            "name": name,
                            "path": os.path.join(path, name).replace("\\", "/"),
                            "type": "file",
                            "size": facts.get("size", "0")
                        })
                
            except (AttributeError, error_perm):
                # Fallback to NLST if MLSD is not available
                names = self.ftp.nlst()
                
                # We don't have file type info with NLST, so we'll make an educated guess
                directories = []
                files = []
                image_exts = (".jpg", ".jpeg", ".png", ".bmp", ".gif")
                
                for name in names:
                    if name in (".", ".."):
                        continue
                        
                    full_path = os.path.join(path, name).replace("\\", "/")
                    
                    # Try to determine if it's a directory by attempting to CWD to it
                    try:
                        curr_dir = self.ftp.pwd()
                        self.ftp.cwd(full_path)
                        self.ftp.cwd(curr_dir)  # Go back to original directory
                        
                        directories.append({
                            "name": name,
                            "path": full_path,
                            "type": "dir"
                        })
                    except error_perm:
                        # If we can't CWD to it, it's probably a file
                        if name.lower().endswith(image_exts):
                            files.append({
                                "name": name,
                                "path": full_path,
                                "type": "file",
                                "size": "unknown"
                            })
            
            # Sort directories and files alphabetically
            directories.sort(key=lambda x: x["name"].lower())
            files.sort(key=lambda x: x["name"].lower())
            
            return {
                "current_path": path,
                "parent_path": os.path.dirname(path) if path != "/" else "/",
                "directories": directories,
                "files": files
            }
            
        except Exception as e:
            logger.error(f"Error listing directory {path}: {e}")
            raise RuntimeError(f"Failed to list directory {path}: {e}")

    def _list_images_recursive(self, path, image_exts=(".jpg", ".jpeg", ".png", ".bmp", ".gif")):
        """
        Recursively collect all image paths under `path`.
        Return list of remote paths (strings).
        """
        if not self.ftp:
            raise RuntimeError("FTP connection is not established")
            
        images = []
        try:
            current_dir = self.ftp.pwd()
            self.ftp.cwd(path)
            
            try:
                # Preferred method: mlsd
                entries = list(self.ftp.mlsd())
            except (AttributeError, error_perm, UnicodeDecodeError) as e:
                # Fallback: nlst (no type info)
                logger.warning(f"MLSD failed for {path} ({e}), falling back to NLST")
                try:
                    names = self.ftp.nlst()
                    entries = [(name, {}) for name in names]
                except UnicodeDecodeError as ue:
                    logger.error(f"Unable to list directory {path} due to encoding issues: {ue}")
                    self.ftp.cwd(current_dir)
                    return images

            for name, facts in entries:
                if name in (".", ".."):
                    continue
                
                try:
                    full_path = f"{path.rstrip('/')}/{name}"
                    entry_type = facts.get("type", None)
                    
                    # If we have type info
                    if entry_type == "dir":
                        images.extend(self._list_images_recursive(full_path, image_exts))
                    elif entry_type == "file" or entry_type is None:
                        # Try to determine if it's a directory by attempting to CWD to it
                        if entry_type is None:
                            try:
                                self.ftp.cwd(full_path)
                                # It's a directory, recurse into it
                                self.ftp.cwd(current_dir)  # Go back to original directory
                                images.extend(self._list_images_recursive(full_path, image_exts))
                                continue
                            except (error_perm, UnicodeDecodeError):
                                # Not a directory or encoding issue, check if it's an image
                                pass
                        
                        # Check if it's an image file
                        if name.lower().endswith(image_exts):
                            images.append(full_path)
                except UnicodeDecodeError as ue:
                    logger.warning(f"Skipping file/directory with encoding issue: {name} in {path}")
                    continue
                except Exception as e:
                    logger.warning(f"Error processing {name} in {path}: {e}")
                    continue
            
            # Restore original directory
            self.ftp.cwd(current_dir)
            
            return images
            
        except Exception as e:
            logger.error(f"Error recursively listing images in {path}: {e}")
            raise RuntimeError(f"Failed to list images in {path}: {e}")

    def _download_image(self, remote_path):
        """Download remote_path to a temporary local file; return local filepath."""
        if not self.ftp:
            raise RuntimeError("FTP connection is not established")
            
        try:
            suffix = os.path.splitext(remote_path)[1] or ".png"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                local_path = tmp_file.name
                self.temp_files.append(local_path)  # Add to list for cleanup

                def callback(data):
                    tmp_file.write(data)

                self.ftp.retrbinary(f"RETR {remote_path}", callback)

            return local_path
        except Exception as e:
            logger.error(f"Error downloading image {remote_path}: {e}")
            raise RuntimeError(f"Failed to download image {remote_path}: {e}")

    def _get_image(self, settings, dimensions):
        """
        Get an image from FTP based on settings.
        Returns a PIL Image object.
        """
        server = settings.get("server")
        if not server:
            raise ValueError("FTP Browser plugin: 'server' option is required")

        port = int(settings.get("port", 21))
        username = settings.get("username", "anonymous")
        password = settings.get("password", "")
        use_tls = bool(settings.get("use_tls", False))
        passive = bool(settings.get("passive", True))
        current_path = settings.get("current_path", "/")
        initial_path = settings.get("initial_path", "/")
        random_mode = bool(settings.get("random_mode", False))
        selected_image = settings.get("selected_image")
        
        # Connect to FTP server
        self._connect_ftp(server, port, username, password, use_tls, passive)
        
        try:
            if random_mode:
                # Get a random image from the current directory
                images = self._list_images_recursive(current_path)
                if not images:
                    raise FileNotFoundError(f"No images found under folder '{current_path}'")
                selected_image = random.choice(images)
            elif not selected_image:
                # No image selected, and not in random mode
                raise ValueError("No image selected and random mode is disabled")
                
            # Download the selected image
            local_path = self._download_image(selected_image)
            
            # Open the image and convert to RGB
            img = Image.open(local_path).convert("RGB")
            
            # Apply EXIF orientation to ensure image is in correct orientation
            try:
                img = ImageOps.exif_transpose(img)
            except Exception as e:
                logger.warning(f"Could not apply EXIF orientation for {selected_image}: {e}")
            
            # Handle vertical images based on user preference
            img_width, img_height = img.size
            display_width, display_height = dimensions
            vertical_handling = settings.get("verticalHandling", "rotate")
            
            # Check if image is portrait and display is landscape
            if img_height > img_width and display_width > display_height:
                if vertical_handling == "rotate":
                    logger.info(f"Rotating vertical image 90° clockwise to fit horizontal display")
                    img = img.rotate(90, expand=True)
                elif vertical_handling == "crop":
                    logger.info(f"Cropping and centering vertical image for horizontal display")
                    # Calculate crop dimensions to match display aspect ratio
                    display_aspect = display_width / display_height
                    img_aspect = img_width / img_height
                    
                    if img_aspect > display_aspect:
                        # Image is wider relative to display, crop width
                        new_width = int(img_height * display_aspect)
                        left = (img_width - new_width) // 2
                        img = img.crop((left, 0, left + new_width, img_height))
                    else:
                        # Image is taller relative to display, crop height
                        new_height = int(img_width / display_aspect)
                        top = (img_height - new_height) // 2
                        img = img.crop((0, top, img_width, top + new_height))
            
            # Resize the image to fit the display dimensions
            if settings.get("padImage", False):
                img = ImageOps.contain(img, dimensions, method=Image.LANCZOS)
            else:
                img = img.resize(dimensions, Image.LANCZOS)
                
            return img
            
        except Exception as e:
            logger.error(f"Error getting image: {e}")
            raise RuntimeError(f"Failed to get image: {e}")

    def generate_image(self, settings, device_config):
        """
        Plugin entry point: Generate an image for display.
        """
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            
        return self._get_image(settings, dimensions)
        
    def generate_settings_template(self):
        """
        Generate settings template parameters.
        """
        template_params = super().generate_settings_template()
        
        # Add FTP browser specific parameters
        template_params["ftp_browser"] = True
        
        return template_params
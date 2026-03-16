import base64
import io 
import math
import os
from pathlib import Path
from typing import Optional, Tuple, Union, List
from PIL import Image
from pillow_heif import register_heif_opener

class ImageProcessor:
    def __init__(self, max_dimension: int = 1024,
                 patch_sizes: Optional[List[int]] = None,
                 max_file_size: int = 100 * 1024 * 1024):
        
        if max_dimension <= 0:
            raise ValueError("max_dimension must be positive")
        self.max_dimension = max_dimension
        self.max_file_size = max_file_size
        self.image_extensions = {
            "JPEG": [
                ".jpg",
                ".jpeg",
                ".jpe",
                ".jif",
                ".jfif",
                ".jfi",
                ".jp2",
                ".j2k",
                ".jpf",
                ".jpx",
                ".jpm",
                ".mj2",
            ],
            "PNG": [".png"],
            "GIF": [".gif"],
            "TIFF": [".tiff", ".tif"],
            "WEBP": [".webp"],
            "HEIF": [".heif", ".heic"],
        }
    def _get_image_type(self, file_path):
        """ Return the image type based on extension
        """
        file_ext = os.path.splitext(file_path)[1]
        if not file_ext.startswith("."):
            file_ext = "." + file_ext
        file_ext = file_ext.lower()
        for file_type, extensions in self.image_extensions.items():
            if file_ext in [ext.lower() for ext in extensions]:
                return file_type
        return None
    
    def _calculate_dimensions(self, width, height):
        """ Calculate dimensions maintaining aspect ratio and patch compatibility 
        """
        scale = min(self.max_dimension / width, self.max_dimension / height)
        
        scaled_width = width * scale
        scaled_height = height * scale
        
        return int(scaled_width), int(scaled_height)

    def _resize_image(self, img):
        """ Resize image ensuring patch compatibility
        """
        new_width, new_height = self._calculate_dimensions(*img.size)
        if new_width != img.width or new_height != img.height:
            return img.resize((new_width, new_height), Image.Resampling.BICUBIC)
        return img

    def route_image(self, file_path):
        """ Process image """
        if os.path.getsize(file_path) > self.max_file_size:
            raise ValueError(f"File exceeds size limit of {self.max_file_size} bytes")
            
        image_type = self._get_image_type(file_path)
        if image_type is None:
            return None
            
        try:
            if image_type == "HEIF":
                register_heif_opener()
                
            with Image.open(file_path) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                    
                if img.width <= 0 or img.height <= 0:
                    raise ValueError("Invalid image dimensions")
                    
                resized = self._resize_image(img)
                
                with io.BytesIO() as buffer:
                    resized.save(buffer, format="PNG")
                    return base64.b64encode(buffer.getvalue()).decode()
                    
        except (IOError, OSError) as e:
            raise ValueError(f"{str(e)}")
            
        return None
        
    def process_image(self, file_path):    
        """ Process an image through the LLM
        """
        file_path = os.path.normpath(file_path)
        encoded = self.route_image(file_path)
        
        if not encoded:
            return None, file_path

        return encoded, file_path

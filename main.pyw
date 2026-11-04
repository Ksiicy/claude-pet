# 双击即可运行（无控制台窗口）；开机自启也指向这个文件
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aoi_pet.launch import run

sys.exit(run())

import os
import shutil
import time
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Flask 应用运行的 URL 和端口
url = 'http://10.16.37.200:5000/SaveFile'
data = {"equipment": "EQ00003369"}

# 监控的文件夹路径
watch_folder = 'C:\\LIMIS'
# 临时文件夹路径，用于存放文件副本
temp_folder = os.path.join(watch_folder, 'temp')

class UploaderHandler(FileSystemEventHandler):
    def __init__(self, url, data):
        super().__init__()
        self.url = url
        self.data = data
        self.uploaded_files = set()  # 记录已上传的文件
        # 确保临时文件夹存在
        os.makedirs(temp_folder, exist_ok=True)

    def on_modified(self, event):
        if event.is_directory:
            return None
        elif event.src_path.endswith(('.xlsx', '.xls')):  # 仅处理 Excel 文件
            file_name = os.path.basename(event.src_path)
            temp_file_path = os.path.join(temp_folder, file_name)
            if event.src_path not in self.uploaded_files:
                if self.try_copy_and_upload(event.src_path, temp_file_path):
                    self.uploaded_files.add(event.src_path)

    def try_copy_and_upload(self, src_path, temp_path):
        try:
            time.sleep(1)
            # 尝试复制文件到临时文件夹
            shutil.copy2(src_path, temp_path)
            # 尝试上传复制的文件
            self.upload_file(temp_path)
            # 上传成功后删除临时文件
            os.remove(temp_path)
            return True
        except PermissionError:
            print(f"无法复制文件 {src_path}，请先关闭表格再试。")
            return False
        except Exception as e:
            print(f"处理文件 {src_path} 时发生错误: {e}")
            # 可以添加重试逻辑，但这里为了简化，直接返回False
            return False

    def upload_file(self, file_path):
        try:
            with open(file_path, 'rb') as f:
                files = {'file': f}
                response = requests.post(self.url, files=files, data=self.data)
                response.raise_for_status()  # 如果响应状态码不是200，将引发HTTPError异常
                print(f"文件 {file_path} 上传成功")
        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP错误: {http_err}")
        except Exception as err:
            print(f"其他错误: {err}")

def start_watching():
    event_handler = UploaderHandler(url, data)
    observer = Observer()
    observer.schedule(event_handler, watch_folder, recursive=False)
    observer.start()
    print(f"数采程序启动成功，请将最新报告存入{watch_folder}")
    try:
        while True:
            time.sleep(1)  # 减少CPU使用率
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    start_watching()
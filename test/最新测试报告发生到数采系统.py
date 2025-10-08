import requests
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Flask 应用运行的 URL 和端口（默认是 127.0.0.1:5000）
url = 'http://10.16.37.200:5000/SaveFile'
data = {"equipment": "EQ00003367"}

# 监控的文件夹路径
watch_folder = 'C:\\LIMIS'

class UploaderHandler(FileSystemEventHandler):
    def __init__(self, url, data):
        super().__init__()
        self.url = url
        self.data = data
        self.uploaded_files = set()  # 用于记录已上传的文件，防止重复上传

    def on_modified(self, event):
        if event.is_directory:
            return None
        elif event.src_path.endswith(('.xlsx', '.xls')):  # 仅处理 Excel 文件
            if event.src_path not in self.uploaded_files:
                try:
                    self.upload_file(event.src_path)
                    self.uploaded_files.add(event.src_path)
                except PermissionError as e:
                    print(f"无法上传文件 {event.src_path}，请先关闭表格再试。")

    def upload_file(self, file_path):
        try:
            with open(file_path, 'rb') as f:
                files = {'file': f}
                print(f"文件 {file_path} 上传成功")
                response = requests.post(self.url, files=files, data=self.data)
                print(f"文件 {file_path} 上传成功")

        except Exception as e:
            print(f"上传文件 {file_path} 时发生错误: {e}，请先关闭表格再试。")
            while True:
                time.sleep(1)
                try:
                    with open(file_path, 'rb') as f:
                        files = {'file': f}
                        response = requests.post(self.url, files=files, data=self.data)
                        print(f"文件 {file_path} 上传成功")
                        break
                except Exception as e:
                    print(f"上传文件 {file_path} 时发生错误: {e}，请先关闭表格再试。")


def start_watching():
    event_handler = UploaderHandler(url, data)
    observer = Observer()
    observer.schedule(event_handler, watch_folder, recursive=False)
    observer.start()
    print(f"数采程序启动成功，请将最新报告存入{watch_folder}")
    try:
        while True:
            time.sleep(1)  # 睡眠1秒，减少CPU使用率
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    start_watching()
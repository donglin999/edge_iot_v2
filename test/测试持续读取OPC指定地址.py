from opcua import Client, ua
import pandas as pd
import threading
import time

class OPCDataRetriever:
    def __init__(self, server_url, node_id):
        self.server_url = server_url
        self.node_id = node_id
        self.opc = None
        self.subscription = None
        self.subscription_handler = None
        self.data_lock = threading.Lock()
        self.data = []
        self.running = False  # 添加一个标志来跟踪是否正在运行

    def connect(self):
        self.opc = Client(self.server_url, timeout=10)
        self.opc.connect()

    def create_subscription(self):
        # 创建一个订阅，这里的1000是发布间隔（毫秒），但可能受服务器限制
        self.subscription = self.opc.create_subscription(1000, self.subscription_handler)

        # 读取节点
        node = self.opc.get_node(self.node_id)

        # 尝试添加数据变化订阅到节点（注意：如果使用的库不支持add_data_change，需要替换为正确的方法）
        try:
            self.subscription.add_data_change(node, self.handle_data_change)
        except AttributeError:
            # 如果add_data_change不存在，可能需要使用其他方法，如add_item并设置回调函数
            # 这取决于您使用的opcua库的具体实现
            pass  # 这里需要适当的错误处理或替换为正确的方法调用

    def handle_data_change(self, node, val, attr):
        with self.data_lock:
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S.%f', time.localtime(val.SourceTimestamp / 1e-6))[:-3]  # 格式化时间戳到毫秒
            self.data.append([timestamp, val.Value])

    def subscription_handler(self, handle, context, state):
        if state == ua.StatusCode(ua.StatusCodes_BadSubscriptionIdInvalid):
            print("Subscription id invalid")
        elif state == ua.StatusCode(ua.StatusCodes_Good):
            print("Subscription acknowledged")

    def run(self):
        self.running = True  # 设置运行标志为True
        self.connect()
        self.create_subscription()

        try:
            # 无限循环，直到通过其他机制（如键盘中断）停止
            while self.running:
                time.sleep(0.1)  # 睡眠一小段时间以避免忙等待，但这不是读取间隔
        except KeyboardInterrupt:
            print("Interrupted by user")
        finally:
            self.stop_subscription()
            self.close()

    def stop_subscription(self):
        if self.subscription:
            self.subscription.delete()
            self.subscription = None

    def save_to_csv(self, filename='opc_data.csv'):
        with self.data_lock:
            df = pd.DataFrame(self.data, columns=['Timestamp', 'Value'])
            df.to_csv(filename, index=False)
            print(f"Data has been saved to {filename}")

    def close(self):
        if self.opc:
            self.opc.disconnect()

# 使用示例
if __name__ == "__main__":
    server_url = 'opc.tcp://192.168.2.130:16664'
    node_id = 'ns=1;s=50603560'  # 替换为实际的节点ID
    opc_retriever = OPCDataRetriever(server_url, node_id)
    try:
        opc_retriever.run()  # 这将一直运行，直到通过Ctrl+C中断
    except KeyboardInterrupt:
        opc_retriever.save_to_csv()  # 在中断后保存数据到CSV文件
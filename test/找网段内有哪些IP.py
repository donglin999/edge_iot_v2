import subprocess
import time
import platform
import threading
import queue  # 注意这里是从 queue 模块导入 Queue 类

def ping(host, result_queue):
    """
    Pings a given host and puts the result into the result_queue.
    """
    # Determine the ping command based on the operating system
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    command = ['ping', param, '1', host]

    # Run the ping command and capture the output
    output = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # Check the output to determine if the host responded
    if '1 packets transmitted, 1 received' in output.stdout or '1 packets received' in output.stdout:
        result_queue.put((host, True))
    else:
        result_queue.put((host, False))

def ping_network(base_ip, start, end, num_threads):
    """
    Pings each IP in the specified range within the given base IP using multiple threads.
    """
    threads = []
    result_queue = queue.Queue()  # 使用 queue.Queue 而不是 threading.Queue

    for i in range(start, end + 1):
        ip = f"{base_ip}.{i}"
        thread = threading.Thread(target=ping, args=(ip, result_queue))
        threads.append(thread)
        thread.start()

    # Wait for all threads to complete
    for thread in threads:
        thread.join()

    # Process and print the results
    while not result_queue.empty():
        ip, is_alive = result_queue.get()
        if is_alive:
            print(f"{ip} 存在")
        else:
            print(f"{ip} 无此IP")

if __name__ == "__main__":
    base_ip = "192.168.3"
    start_ip = 0
    end_ip = 255
    num_threads = 10  # You can adjust the number of threads to use

    # Note: In this specific case, we are starting a thread for each IP,
    # so num_threads is not strictly necessary for controlling the number
    # of concurrent threads. However, it can be useful if you want to limit
    # the number of concurrent pings for resource management purposes.
    # If you want to limit the number of concurrent threads, you would need
    # to implement a thread pool or use a library like concurrent.futures.ThreadPoolExecutor.

    # Since we are starting all threads at once, we simply pass a placeholder for num_threads.
    ping_network(base_ip, start_ip, end_ip, num_threads)
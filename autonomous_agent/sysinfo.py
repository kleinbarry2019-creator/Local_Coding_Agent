import psutil

print('RAM Usage:', psutil.virtual_memory().percent)
print('CPU Usage:', psutil.cpu_percent())

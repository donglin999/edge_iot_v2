from influxdb_client import InfluxDBClient

bucket = "XingTan"
org = "IMRC"
token = "lpI9utYNezbsHpImJlDnoPLzaH8z52Jce0FTBwPgxgSK43nllJ71c9kB-qTz4-T1MGPKcszB5AOzu6OzpUEnRQ=="

client = InfluxDBClient(url="http://10.79.223.96:8086", token=token)

delete_api = client.delete_api()

"""
Delete Data
"""
start = "2024-10-20T00:00:00Z"
stop = "2024-10-27T00:00:00Z"

delete_api.delete(start, stop, '_measurement="AN180600042400024"', bucket=bucket, org=org)

"""
Close client
"""
client.close()
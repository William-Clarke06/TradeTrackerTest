import requests
s = requests.Session()
s.trust_env = False
r = s.get('http://openinsider.com/latest-cluster-buys')
print(r.status_code)
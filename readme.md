# 部署

```sh
mkdir -p /data/cert-monitor/{conf,ssh}
cd /data/cert-monitor/

#准备配置文件
#vim conf/config.yaml

#准备ssh密钥
#vim ssh/id_秘钥类型

docker compose up -d
```
# resource "helm_release" "redis" {
#   name       = "my-redis"
#   repository = "https://charts.bitnami.com/bitnami"
#   chart      = "redis"
#   namespace  = "default"
#   values = [
#     file("${path.module}/../values/redis.yaml")
#   ]
# }
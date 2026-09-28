resource "kubernetes_daemonset" "timezone_setup" {
  metadata {
    name      = "timezone-setup"
    namespace = "kube-system"
    labels = {
      app = "timezone-setup"
    }
  }

  spec {
    selector {
      match_labels = {
        app = "timezone-setup"
      }
    }

    template {
      metadata {
        labels = {
          app = "timezone-setup"
        }
      }

      spec {
        host_network = true
        host_pid     = true

        toleration {
          key    = "node-role.kubernetes.io/control-plane"
          effect = "NoSchedule"
        }

        toleration {
          key    = "node-role.kubernetes.io/master"
          effect = "NoSchedule"
        }

        container {
          name  = "timezone-setup"
          image = "busybox:1.36"

          command = [
            "/bin/sh",
            "-c",
            <<-EOT
            # 호스트의 /etc/localtime을 Asia/Seoul로 설정
            if [ ! -f /host/etc/localtime ] || [ "$(readlink /host/etc/localtime)" != "/usr/share/zoneinfo/Asia/Seoul" ]; then
              echo "Setting timezone to Asia/Seoul"
              ln -sf /usr/share/zoneinfo/Asia/Seoul /host/etc/localtime
              echo "Asia/Seoul" > /host/etc/timezone
            fi
            # DaemonSet이 계속 실행되도록 대기
            sleep infinity
            EOT
          ]

          security_context {
            privileged = true
          }

          volume_mount {
            name       = "host-etc"
            mount_path = "/host/etc"
          }

          volume_mount {
            name       = "host-usr-share-zoneinfo"
            mount_path = "/usr/share/zoneinfo"
            read_only  = true
          }

          resources {
            requests = {
              cpu    = "10m"
              memory = "16Mi"
            }
            limits = {
              cpu    = "50m"
              memory = "32Mi"
            }
          }
        }

        volume {
          name = "host-etc"
          host_path {
            path = "/etc"
          }
        }

        volume {
          name = "host-usr-share-zoneinfo"
          host_path {
            path = "/usr/share/zoneinfo"
          }
        }
      }
    }
  }
} 

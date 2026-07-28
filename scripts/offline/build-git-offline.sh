#!/usr/bin/env bash
# ============================================================================
# 构建【离线 git 环境 一键安装包】到 dist/offline/git-offline-amd64/。
# 在联网构建机上运行;产物目录拷到无外网的生产机(须已有 docker,能连内网
# GitLab),跑 ./install-git.sh 后宿主机就有可用的 git 命令。
#
# 思路:不装发行版软件包(依赖链因 distro 而异,离线极易翻车),把 git 放进
# alpine/git 容器镜像,宿主机 /usr/local/bin/git 是一个透明包装脚本 ——
# 挂载当前目录与 HOME(gitconfig/凭据),host 网络直连内网 GitLab。
# 对使用者来说就是普通 git:clone/pull/push/log 全部照常。
# ============================================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
OUT="dist/offline/git-offline-amd64"
IMAGE="alpine/git:latest"

echo "==> [1/3] 拉取 amd64 镜像并导出"
docker pull --platform linux/amd64 "$IMAGE"
rm -rf "$OUT" && mkdir -p "$OUT"
docker save --platform linux/amd64 "$IMAGE" -o "$OUT/git-image.tar"
( cd "$OUT" && shasum -a 256 git-image.tar > git-image.tar.sha256 )

echo "==> [2/3] 生成安装器与包装脚本"
cat > "$OUT/install-git.sh" <<'INSTALLER'
#!/usr/bin/env bash
# 一键安装离线 git(容器方案)。在本目录内以 root 运行:./install-git.sh
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "!! 需要 docker(生产机应已有)"; exit 1; }

echo "==> 校验并加载 git 镜像"
if [ -f git-image.tar.sha256 ]; then
  shasum -a 256 -c git-image.tar.sha256 2>/dev/null || sha256sum -c git-image.tar.sha256
fi
docker load -i git-image.tar

echo "==> 安装 /usr/local/bin/git 包装命令"
cp git-wrapper.sh /usr/local/bin/git
chmod +x /usr/local/bin/git

# 凭据存储:首次输入 GitLab 账号密码后落在 ~/.git-credentials,之后免输。
touch "$HOME/.gitconfig" "$HOME/.git-credentials"
/usr/local/bin/git config --global credential.helper store
# 内网 GitLab 常用自签/内部 CA 证书,容器里没有对应 CA —— 内网环境关闭校验。
/usr/local/bin/git config --global http.sslVerify false
# 挂载目录的属主与容器内 root 不一致时 git 会拒绝操作,内网单人生产机直接放行。
/usr/local/bin/git config --global --add safe.directory '*'

echo "==> 自检"
/usr/local/bin/git version

echo ""
echo "完成。用法与普通 git 无异,例如:"
echo "  git clone https://git.midea.com/DEP-IMRC/daxm/data_acquisition_python.git"
echo "  (首次 clone 会提示输入 GitLab 账号密码,之后记住免输)"
echo "提示:未配置提交人时先执行:"
echo "  git config --global user.name '你的名字'"
echo "  git config --global user.email '你@midea.com'"
INSTALLER
chmod +x "$OUT/install-git.sh"

cat > "$OUT/git-wrapper.sh" <<'WRAPPER'
#!/usr/bin/env bash
# 透明 git 包装:实际执行发生在 alpine/git 容器里。
# 挂载当前目录(同绝对路径)与 HOME(gitconfig/凭据/ssh),host 网络直连内网。
set -eo pipefail
TTY=""
[ -t 0 ] && [ -t 1 ] && TTY="-t"   # 交互式(输密码)才挂 tty
# shellcheck disable=SC2086 — $TTY 故意不加引号(空时消失)
exec docker run --rm -i $TTY \
  --network host \
  -v "$PWD:$PWD" -w "$PWD" \
  -v "$HOME:$HOME" \
  -e HOME="$HOME" \
  alpine/git:latest "$@"
WRAPPER
chmod +x "$OUT/git-wrapper.sh"

cat > "$OUT/README.md" <<'DOC'
# 离线 git 环境(容器方案,amd64)

前提:目标机已有 docker,能连内网 GitLab(无需外网)。

    ./install-git.sh

装完即有全局 `git` 命令,clone/pull/push/log 与原生无异。首次访问 GitLab
输一次账号密码后记住(credential store)。已默认关闭 https 证书校验(内网
自签场景);已配置 safe.directory 放行。

原理:git 运行在 alpine/git 容器内(镜像已随包离线携带),宿主机的 git 命令
是透明包装 —— 挂载当前目录与 HOME、host 网络。卸载:rm /usr/local/bin/git。

限制:操作的仓库路径需在当前目录或 HOME 之下(包装只挂载这两处;生产部署
clone 到 /root 或工作目录下即可)。
DOC

echo "==> [3/3] 完成"
ls -la "$OUT"
du -sh "$OUT"

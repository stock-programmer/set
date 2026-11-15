#!/bin/bash
# Finder runs .command files in Terminal. Use the folder containing this copy.
cd -P -- "$(dirname "$0")" || exit 1
printf '%s\n' '片场 · 短剧制作工作台'

# Terminal launched by Finder may not have loaded the user's Homebrew PATH.
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin"
export PYTHONUTF8=1
python=""
for candidate in "$(command -v python3)" /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/{Current,3.*}/bin/python3 \
        /opt/homebrew/opt/python@3.*/libexec/bin/python3 /usr/local/opt/python@3.*/libexec/bin/python3; do
    [[ -x "$candidate" ]] || continue
    # Apple's placeholder can open the Command Line Tools installer if executed.
    if [[ "$candidate" == /usr/bin/python3 ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' </dev/null >/dev/null 2>&1; then
        python=$candidate
        break
    fi
done

if [[ -z "$python" ]]; then
    url=https://www.python.org/downloads/macos/
    printf '首次启动需要 Python 3.11 或更高版本。请安装官网的 macOS universal2 安装包，再双击启动片场。\n%s\n' "$url"
    if [[ -t 0 ]]; then
        read -r -p '现在打开 Python 官方下载页吗？[Y/n] ' answer
        case "$answer" in ''|[Yy]|[Yy][Ee][Ss]|是) /usr/bin/open "$url" ;; esac
    fi
    exit 1
fi

launch_args=(--configure)
open_browser=true
for arg in "$@"; do
    case "$arg" in
        --no-browser) open_browser=false ;;
        *) launch_args+=("$arg") ;;
    esac
done
if [[ "$open_browser" == true ]]; then launch_args+=(--open-browser); fi
"$python" -m tools.launch_workbench "${launch_args[@]}"
status=$?
if [[ $status -ne 0 && -t 0 ]]; then read -r -p '看完上方提示后按回车关闭此窗口。' _; fi
exit "$status"

//go:build windows

package media

import (
	"os"
	"os/exec"
	"strconv"
	"syscall"
)

// windowsCREATE_NEW_PROCESS_GROUP 是 CreateProcess 的 CreationFlags 之一。
//
// 直接写字面量而不是引入 golang.org/x/sys/windows：这个包当前只是间接依赖，
// 为单个常量把它提升为直接依赖并不划算。
const windowsCREATE_NEW_PROCESS_GROUP = 0x00000200

// setupProcAttr 为子进程建立独立的进程组。
//
// Windows 没有 POSIX 意义上的进程组信号，但 CREATE_NEW_PROCESS_GROUP 让
// 子进程脱离父进程的 Ctrl+C 广播域，便于后续用 taskkill /T 递归清理。
func setupProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windowsCREATE_NEW_PROCESS_GROUP,
	}
}

// killTree 递归强杀整棵进程树。
//
// Windows 上 kernel32!TerminateProcess 只作用于单个进程，没有「杀进程组」
// 的系统调用。`taskkill /T /F` 是官方提供的递归终止入口，因此这里借道它。
//
// 失败是**可接受**的退化路径：任何一种原因导致 taskkill 不可用
// （PATH 异常、权限不足、进程已退出），都会回退到单进程 Kill，
// 至少保证直接子进程不会残留。这与 POSIX 侧的回退策略保持一致。
func killTree(p *os.Process) error {
	if p == nil {
		return nil
	}
	// 忽略 taskkill 自身的退出码：进程可能早已退出（那正是我们想要的），
	// 也可能工具不可用。两种情况下都不应让取消路径报错。
	_ = exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(p.Pid)).Run()
	return p.Kill()
}

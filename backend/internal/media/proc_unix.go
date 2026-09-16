//go:build !windows

package media

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

// setupProcAttr 把子进程放进**独立的进程组**。
//
// 这是「取消时能杀干净」的前提：ffmpeg 在处理复杂滤镜图或调用外部程序时
// 可能派生后代进程。若只 kill 直接子进程，后代会被 init 收养并继续吃 CPU ——
// 表现为「任务已取消，但机器依然满载」这种极难排查的故障。
// 放进独立进程组后，就能对整组投递信号。
func setupProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid: true, // 新建进程组，pgid = 子进程 pid
	}
}

// killTree 强杀**整棵进程树**。
//
// 用负的 pid 调 kill 表示「向该进程组内所有进程投递信号」。
// 先 SIGKILL 后 SIGTERM 的取舍：这里是超时/取消路径，要的是确定性清理，
// 而不是给 ffmpeg 机会写完文件（写到一半的产物本来就会被判为无效）。
func killTree(p *os.Process) error {
	if p == nil {
		return nil
	}
	// 负号 = 整个进程组。子进程已被回收（ESRCH）视为成功 —— 目标已达成。
	if err := syscall.Kill(-p.Pid, syscall.SIGKILL); err == nil || errors.Is(err, syscall.ESRCH) {
		return nil
	}
	// 进程组可能因 Setpgid 失败而不存在，退回单进程杀。
	return p.Kill()
}

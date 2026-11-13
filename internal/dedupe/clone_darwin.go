package dedupe

import (
	"io/fs"

	"golang.org/x/sys/unix"
)

func clonefile(src, dst string) error {
	if err := unix.Clonefile(src, dst, 0); err != nil {
		return &fs.PathError{Op: "clonefile", Path: dst, Err: err}
	}
	return nil
}

// DedupeRange 只在 Linux 上可用。
func DedupeRange(src, dst string, length int64) (bool, error) {
	return false, unix.ENOTSUP
}

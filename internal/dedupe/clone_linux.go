package dedupe

import (
	"fmt"
	"io/fs"
	"os"

	"golang.org/x/sys/unix"
)

// clonefile 只在 macOS 上可用。
func clonefile(src, dst string) error { return unix.ENOTSUP }

// dedupeChunk：Btrfs 单次请求最多 16 MiB。
const dedupeChunk = 16 * MiB

// DedupeRange 让 dst 共用 src 的数据块；内容不同时返回 false。
func DedupeRange(src, dst string, length int64) (bool, error) {
	source, err := os.Open(src)
	if err != nil {
		return false, err
	}
	defer source.Close()
	target, err := os.OpenFile(dst, os.O_RDWR, 0)
	if err != nil {
		return false, err
	}
	defer target.Close()
	for offset := int64(0); offset < length; {
		size := min(int64(dedupeChunk), length-offset)
		request := unix.FileDedupeRange{
			Src_offset: uint64(offset), Src_length: uint64(size),
			Info: []unix.FileDedupeRangeInfo{{Dest_fd: int64(target.Fd()), Dest_offset: uint64(offset)}},
		}
		if err := unix.IoctlFileDedupeRange(int(source.Fd()), &request); err != nil {
			return false, &fs.PathError{Op: "FIDEDUPERANGE", Path: dst, Err: err}
		}
		info := request.Info[0]
		if info.Status == unix.FILE_DEDUPE_RANGE_DIFFERS {
			return false, nil
		}
		if info.Status < 0 {
			return false, &fs.PathError{Op: "FIDEDUPERANGE", Path: dst, Err: unix.Errno(-info.Status)}
		}
		if info.Bytes_deduped == 0 {
			return false, fmt.Errorf("%s：内核没有去重任何数据", dst)
		}
		offset += int64(info.Bytes_deduped)
	}
	return true, nil
}

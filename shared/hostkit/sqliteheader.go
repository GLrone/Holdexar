package hostkit

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"os"
)

const (
	sqliteHeaderMagic = "SQLite format 3\x00"
	sqliteHeaderSize  = 100
	walHeaderSize     = 32
	walFrameHdrSize   = 24
	walMagicLE        = 0x377f0682
	walMagicBE        = 0x377f0683
)

func walChecksum(data []byte, s0, s1 uint32, bigEndian bool) (uint32, uint32) {
	if bigEndian {
		for i := 0; i+8 <= len(data); i += 8 {
			s0 += binary.BigEndian.Uint32(data[i:i+4]) + s1
			s1 += binary.BigEndian.Uint32(data[i+4:i+8]) + s0
		}
	} else {
		for i := 0; i+8 <= len(data); i += 8 {
			s0 += binary.LittleEndian.Uint32(data[i:i+4]) + s1
			s1 += binary.LittleEndian.Uint32(data[i+4:i+8]) + s0
		}
	}
	return s0, s1
}

func validSQLitePageSize(sz uint32) bool {
	return sz >= 512 && sz <= 65536 && (sz&(sz-1)) == 0
}

// ReadSQLiteUserVersion reads PRAGMA user_version from Page 1 of dbPath and any committed frames in dbPath+"-wal".
func ReadSQLiteUserVersion(dbPath string) (int, error) {
	dbFile, err := os.Open(dbPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) || os.IsNotExist(err) {
			return 0, nil
		}
		return 0, err
	}
	defer dbFile.Close()

	var hdr [sqliteHeaderSize]byte
	n, err := io.ReadFull(dbFile, hdr[:])
	committedVersion := 0
	if err != nil {
		if errors.Is(err, io.EOF) && n == 0 {
			committedVersion = 0
		} else {
			return 0, errors.New("SQLite 文件头损坏或长度不足")
		}
	} else {
		if string(hdr[:16]) != sqliteHeaderMagic {
			return 0, errors.New("不是有效的 SQLite 数据库文件")
		}
		rawPageSize := uint32(binary.BigEndian.Uint16(hdr[16:18]))
		if rawPageSize == 1 {
			rawPageSize = 65536
		}
		if !validSQLitePageSize(rawPageSize) {
			return 0, errors.New("SQLite 页大小无效")
		}
		committedVersion = int(int32(binary.BigEndian.Uint32(hdr[60:64])))
	}

	walFile, err := os.Open(dbPath + "-wal")
	if err != nil {
		if errors.Is(err, os.ErrNotExist) || os.IsNotExist(err) {
			return committedVersion, nil
		}
		return 0, err
	}
	defer walFile.Close()

	var walHdr [walHeaderSize]byte
	if _, err := io.ReadFull(walFile, walHdr[:]); err != nil {
		return committedVersion, nil
	}

	magic := binary.BigEndian.Uint32(walHdr[0:4])
	if magic != walMagicLE && magic != walMagicBE {
		return committedVersion, nil
	}
	bigEndian := magic == walMagicBE
	pageSize := binary.BigEndian.Uint32(walHdr[8:12])
	if !validSQLitePageSize(pageSize) {
		return committedVersion, nil
	}

	s0, s1 := walChecksum(walHdr[0:24], 0, 0, bigEndian)
	if s0 != binary.BigEndian.Uint32(walHdr[24:28]) || s1 != binary.BigEndian.Uint32(walHdr[28:32]) {
		return committedVersion, nil
	}

	salt1 := walHdr[16:20]
	salt2 := walHdr[20:24]
	pageBuf := make([]byte, pageSize)
	var frameHdr [walFrameHdrSize]byte

	pendingVersion := committedVersion
	hasPending := false

	for {
		if _, err := io.ReadFull(walFile, frameHdr[:]); err != nil {
			break
		}
		if _, err := io.ReadFull(walFile, pageBuf); err != nil {
			break
		}
		if !bytes.Equal(frameHdr[8:12], salt1) || !bytes.Equal(frameHdr[12:16], salt2) {
			break
		}
		s0, s1 = walChecksum(frameHdr[0:8], s0, s1, bigEndian)
		s0, s1 = walChecksum(pageBuf, s0, s1, bigEndian)
		if s0 != binary.BigEndian.Uint32(frameHdr[16:20]) || s1 != binary.BigEndian.Uint32(frameHdr[20:24]) {
			break
		}

		pageNumber := binary.BigEndian.Uint32(frameHdr[0:4])
		dbSize := binary.BigEndian.Uint32(frameHdr[4:8])
		if pageNumber == 1 {
			pendingVersion = int(int32(binary.BigEndian.Uint32(pageBuf[60:64])))
			hasPending = true
		}
		if dbSize != 0 && hasPending {
			committedVersion = pendingVersion
			hasPending = false
		}
	}

	return committedVersion, nil
}

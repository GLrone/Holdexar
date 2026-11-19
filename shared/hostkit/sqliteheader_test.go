package hostkit

import (
	"encoding/binary"
	"path/filepath"
	"testing"
)

func buildSQLiteDBPage1(pageSize int, userVersion int32) []byte {
	page := make([]byte, pageSize)
	copy(page[0:16], []byte("SQLite format 3\x00"))
	rawSize := uint16(pageSize)
	if pageSize == 65536 {
		rawSize = 1
	}
	binary.BigEndian.PutUint16(page[16:18], rawSize)
	page[18] = 2
	page[19] = 2
	page[21] = 64
	page[22] = 32
	page[23] = 32
	binary.BigEndian.PutUint32(page[28:32], 1)
	binary.BigEndian.PutUint32(page[60:64], uint32(userVersion))
	return page
}

type walFrameSpec struct {
	pageNumber   uint32
	dbSize       uint32
	userVersion  int32
	corruptSalt  bool
	corruptCksum bool
}

func buildSQLiteWAL(magic uint32, pageSize int, salt1, salt2 uint32, frames []walFrameSpec) []byte {
	bigEndian := magic == walMagicBE
	var hdr [walHeaderSize]byte
	binary.BigEndian.PutUint32(hdr[0:4], magic)
	binary.BigEndian.PutUint32(hdr[4:8], 3007000)
	binary.BigEndian.PutUint32(hdr[8:12], uint32(pageSize))
	binary.BigEndian.PutUint32(hdr[12:16], 1)
	binary.BigEndian.PutUint32(hdr[16:20], salt1)
	binary.BigEndian.PutUint32(hdr[20:24], salt2)
	s0, s1 := walChecksum(hdr[0:24], 0, 0, bigEndian)
	binary.BigEndian.PutUint32(hdr[24:28], s0)
	binary.BigEndian.PutUint32(hdr[28:32], s1)

	out := append([]byte(nil), hdr[:]...)
	for _, f := range frames {
		var fhdr [walFrameHdrSize]byte
		binary.BigEndian.PutUint32(fhdr[0:4], f.pageNumber)
		binary.BigEndian.PutUint32(fhdr[4:8], f.dbSize)
		s1Val := salt1
		if f.corruptSalt {
			s1Val ^= 0xffffffff
		}
		binary.BigEndian.PutUint32(fhdr[8:12], s1Val)
		binary.BigEndian.PutUint32(fhdr[12:16], salt2)

		var page []byte
		if f.pageNumber == 1 {
			page = buildSQLiteDBPage1(pageSize, f.userVersion)
		} else {
			page = make([]byte, pageSize)
			binary.BigEndian.PutUint32(page[0:4], f.pageNumber)
		}

		s0, s1 = walChecksum(fhdr[0:8], s0, s1, bigEndian)
		s0, s1 = walChecksum(page, s0, s1, bigEndian)
		c0 := s0
		if f.corruptCksum {
			c0 ^= 1
		}
		binary.BigEndian.PutUint32(fhdr[16:20], c0)
		binary.BigEndian.PutUint32(fhdr[20:24], s1)

		out = append(out, fhdr[:]...)
		out = append(out, page...)
	}
	return out
}

func TestReadSQLiteUserVersionMissingEmptyAndCorrupt(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "missing.db")
	if v, err := ReadSQLiteUserVersion(missing); err != nil || v != 0 {
		t.Fatalf("missing DB = (%d, %v), want (0, nil)", v, err)
	}

	empty := filepath.Join(dir, "empty.db")
	writeTestFile(t, empty, nil)
	if v, err := ReadSQLiteUserVersion(empty); err != nil || v != 0 {
		t.Fatalf("0-byte DB = (%d, %v), want (0, nil)", v, err)
	}

	// 0-byte main DB with committed Page 1 in WAL (fresh WAL-mode database before first checkpoint)
	walData := buildSQLiteWAL(walMagicLE, 4096, 0x11223344, 0x55667788, []walFrameSpec{
		{pageNumber: 1, dbSize: 1, userVersion: 42},
	})
	writeTestFile(t, empty+"-wal", walData)
	if v, err := ReadSQLiteUserVersion(empty); err != nil || v != 42 {
		t.Fatalf("0-byte DB with committed WAL = (%d, %v), want (42, nil)", v, err)
	}

	short := filepath.Join(dir, "short.db")
	writeTestFile(t, short, []byte("SQLite format 3\x00short"))
	if _, err := ReadSQLiteUserVersion(short); err == nil {
		t.Fatal("truncated DB header accepted")
	}

	badMagic := filepath.Join(dir, "bad.db")
	writeTestFile(t, badMagic, make([]byte, 128))
	if _, err := ReadSQLiteUserVersion(badMagic); err == nil {
		t.Fatal("invalid magic accepted")
	}

	db64k := filepath.Join(dir, "page64k.db")
	writeTestFile(t, db64k, buildSQLiteDBPage1(65536, 37))
	if v, err := ReadSQLiteUserVersion(db64k); err != nil || v != 37 {
		t.Fatalf("64k page DB = (%d, %v), want (37, nil)", v, err)
	}
}

func TestReadSQLiteUserVersionWALCommitUncommittedSaltAndChecksum(t *testing.T) {
	for _, tc := range []struct {
		name  string
		magic uint32
	}{
		{"little-endian-0x377f0682", walMagicLE},
		{"big-endian-0x377f0683", walMagicBE},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			dbPath := filepath.Join(dir, "holdexar.db")
			writeTestFile(t, dbPath, buildSQLiteDBPage1(4096, 30))

			// Multi-frame committed tx (30 -> 32), followed by uncommitted trailing Page 1 (99)
			walBytes := buildSQLiteWAL(tc.magic, 4096, 0xaabbccdd, 0x10203040, []walFrameSpec{
				{pageNumber: 1, dbSize: 0, userVersion: 32},
				{pageNumber: 2, dbSize: 2},
				{pageNumber: 1, dbSize: 0, userVersion: 99},
			})
			writeTestFile(t, dbPath+"-wal", walBytes)
			if v, err := ReadSQLiteUserVersion(dbPath); err != nil || v != 32 {
				t.Fatalf("committed+uncommitted WAL = (%d, %v), want (32, nil)", v, err)
			}

			// Stale recycled frame with mismatched salt after valid commit (32 -> 35, then bad-salt 88)
			walSaltMismatch := buildSQLiteWAL(tc.magic, 4096, 0xaabbccdd, 0x10203040, []walFrameSpec{
				{pageNumber: 1, dbSize: 1, userVersion: 35},
				{pageNumber: 1, dbSize: 1, userVersion: 88, corruptSalt: true},
			})
			writeTestFile(t, dbPath+"-wal", walSaltMismatch)
			if v, err := ReadSQLiteUserVersion(dbPath); err != nil || v != 35 {
				t.Fatalf("salt mismatch WAL = (%d, %v), want (35, nil)", v, err)
			}

			// Corrupted checksum frame stops scan and keeps prior committed version (36, ignores 77 and subsequent 78)
			walBadCksum := buildSQLiteWAL(tc.magic, 4096, 0xaabbccdd, 0x10203040, []walFrameSpec{
				{pageNumber: 1, dbSize: 1, userVersion: 36},
				{pageNumber: 1, dbSize: 1, userVersion: 77, corruptCksum: true},
				{pageNumber: 1, dbSize: 1, userVersion: 78},
			})
			writeTestFile(t, dbPath+"-wal", walBadCksum)
			if v, err := ReadSQLiteUserVersion(dbPath); err != nil || v != 36 {
				t.Fatalf("corrupt checksum WAL = (%d, %v), want (36, nil)", v, err)
			}

			// Purely uncommitted WAL preserves main DB user_version (30)
			walUncommittedOnly := buildSQLiteWAL(tc.magic, 4096, 0xaabbccdd, 0x10203040, []walFrameSpec{
				{pageNumber: 1, dbSize: 0, userVersion: 55},
				{pageNumber: 3, dbSize: 0},
			})
			writeTestFile(t, dbPath+"-wal", walUncommittedOnly)
			if v, err := ReadSQLiteUserVersion(dbPath); err != nil || v != 30 {
				t.Fatalf("uncommitted-only WAL = (%d, %v), want (30, nil)", v, err)
			}
		})
	}
}

package backup

import (
	"compress/gzip"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode"

	"holdexar/server-go/internal/store"
)

const Job = "auto_backup"
const localTime = "2006-01-02T15:04:05.999999"
const metadataSuffix = ".gz.json"

var ErrName = errors.New("备份文件名无效")

type Service struct {
	db     *sql.DB
	dbPath string
	dir    string
	now    func() time.Time
	gate   *store.WriteGate
}

func New(db *sql.DB, dbPath string, now func() time.Time, gate *store.WriteGate) *Service {
	return &Service{db: db, dbPath: dbPath, dir: filepath.Join(filepath.Dir(dbPath), "backups"), now: now, gate: gate}
}

type Item struct {
	Name      string `json:"name"`
	SizeBytes int64  `json:"sizeBytes"`
	CreatedAt string `json:"createdAt"`
}

type metadata struct {
	SHA256   string `json:"sha256"`
	Games    int64  `json:"games"`
	RawBytes int64  `json:"rawBytes"`
}

type Result struct {
	Item
	metadata
	Path        string `json:"path"`
	IntegrityOK bool   `json:"integrityOk"`
}

type archive struct {
	Item
	modified time.Time
}

func backupName(name string) bool {
	if strings.Contains(name, "..") || (!strings.HasSuffix(name, ".db") && !strings.HasSuffix(name, ".db.gz")) {
		return false
	}
	for _, ch := range name {
		if !unicode.IsLetter(ch) && !unicode.IsNumber(ch) && !strings.ContainsRune("-_.", ch) {
			return false
		}
	}
	return true
}

func (s *Service) archives() ([]archive, error) {
	entries, err := os.ReadDir(s.dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var archives []archive
	for _, entry := range entries {
		if !backupName(entry.Name()) || !entry.Type().IsRegular() {
			continue
		}
		info, err := entry.Info()
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
		modified := info.ModTime()
		archives = append(archives, archive{Item{entry.Name(), info.Size(), modified.Local().Format(localTime)}, modified})
	}
	slices.SortFunc(archives, func(a, b archive) int {
		if cmp := b.modified.Compare(a.modified); cmp != 0 {
			return cmp
		}
		return strings.Compare(b.Name, a.Name)
	})
	return archives, nil
}

func (s *Service) List() ([]Item, error) {
	archives, err := s.archives()
	items := make([]Item, 0, len(archives))
	for _, item := range archives {
		items = append(items, item.Item)
	}
	return items, err
}

func (s *Service) Automatic(ctx context.Context) (result Result, skipped bool, err error) {
	archives, err := s.archives()
	if err != nil || len(archives) > 0 && s.now().Sub(archives[0].modified) < 24*time.Hour {
		return result, err == nil, err
	}
	return s.create(ctx, "", false)
}

func (s *Service) Create(ctx context.Context, label string) (Result, error) {
	for _, ch := range label {
		if !unicode.IsLetter(ch) && !unicode.IsNumber(ch) && ch != '-' && ch != '_' {
			return Result{}, ErrName
		}
	}
	result, _, err := s.create(ctx, label, true)
	return result, err
}

func (s *Service) create(ctx context.Context, label string, manual bool) (result Result, skipped bool, err error) {
	if err := ctx.Err(); err != nil {
		return result, false, err
	}
	if _, err := os.Stat(s.dbPath); err != nil {
		return result, false, err
	}
	end, err := store.BeginBackupMaintenance(s.dbPath)
	if err != nil {
		return result, false, err
	}
	defer func() { err = errors.Join(err, end()) }()
	now := s.now()
	if !manual {
		archives, err := s.archives()
		if err != nil || len(archives) > 0 && now.Sub(archives[0].modified) < 24*time.Hour {
			return result, err == nil, err
		}
	}
	name := strings.TrimSuffix(filepath.Base(s.dbPath), filepath.Ext(s.dbPath)) + "-" + now.Format("20060102-150405")
	if label != "" {
		name += "-" + label
	}
	if manual && label != "manual" {
		name += "-manual"
	}
	name += ".db.gz"
	path := filepath.Join(s.dir, name)
	if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
		if err == nil {
			err = fmt.Errorf("备份文件已存在：%s", name)
		}
		return result, false, err
	}
	snapshot, err := os.CreateTemp(s.dir, ".snapshot-*.tmp")
	if err != nil {
		return result, false, err
	}
	defer os.Remove(snapshot.Name())
	if err := snapshot.Close(); err != nil {
		return result, false, err
	}
	if err := store.Snapshot(ctx, s.db, snapshot.Name()); err != nil {
		return result, false, fmt.Errorf("无法生成数据库快照：%w", err)
	}
	ok, games, err := inspectDatabase(ctx, snapshot.Name(), false)
	if err != nil || !ok {
		return result, false, errors.Join(errors.New("数据库备份自检未通过"), err)
	}
	info, err := os.Stat(snapshot.Name())
	if err != nil {
		return result, false, err
	}
	compressed, err := os.CreateTemp(s.dir, ".archive-*.tmp")
	if err != nil {
		return result, false, err
	}
	defer os.Remove(compressed.Name())
	digest, err := compress(ctx, snapshot.Name(), compressed)
	if err != nil {
		return result, false, err
	}
	archiveInfo, err := os.Stat(compressed.Name())
	if err != nil {
		return result, false, err
	}
	meta := metadata{digest, games, info.Size()}
	if err := publish(ctx, compressed.Name(), path, meta); err != nil {
		return result, false, err
	}
	result = Result{Item{name, archiveInfo.Size(), archiveInfo.ModTime().Local().Format(localTime)}, meta, path, true}
	return result, false, s.rotate()
}

type contextReader struct {
	ctx context.Context
	r   io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.r.Read(p)
}

func compress(ctx context.Context, snapshot string, destination *os.File) (string, error) {
	source, err := os.Open(snapshot)
	if err != nil {
		return "", errors.Join(err, destination.Close())
	}
	defer source.Close()
	hash := sha256.New()
	writer := gzip.NewWriter(io.MultiWriter(destination, hash))
	_, copyErr := io.Copy(writer, contextReader{ctx, source})
	if err := errors.Join(copyErr, writer.Close(), destination.Sync(), destination.Close()); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func publish(ctx context.Context, staging, path string, meta metadata) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".archive-meta-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if err := errors.Join(json.NewEncoder(file).Encode(meta), file.Sync(), file.Close()); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), path+metadataSuffix); err != nil {
		return err
	}
	if err := os.Rename(staging, path); err != nil {
		return errors.Join(err, os.Remove(path+metadataSuffix))
	}
	return nil
}

func inspectDatabase(ctx context.Context, path string, deep bool) (bool, int64, error) {
	db, err := store.OpenReadOnly(path)
	if err != nil {
		return false, 0, err
	}
	defer db.Close()
	result, err := store.CheckIntegrity(ctx, db, deep)
	if err != nil || result != "ok" {
		return false, 0, err
	}
	var hasGames bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name='games')`).Scan(&hasGames); err != nil || !hasGames {
		return err == nil, 0, err
	}
	var games int64
	err = db.QueryRowContext(ctx, `SELECT count(*) FROM games`).Scan(&games)
	return err == nil, games, err
}

func (s *Service) rotate() error {
	items, err := s.archives()
	if err != nil {
		return err
	}
	source, err := os.Stat(s.dbPath)
	if err != nil {
		return err
	}
	var total int64
	var automatic, manual int
	for _, item := range items {
		if strings.HasSuffix(strings.TrimSuffix(item.Name, ".gz"), "-manual.db") {
			manual++
			if manual <= 1 {
				continue
			}
		} else if automatic < 2 || automatic < 5 && total+item.SizeBytes <= 2*source.Size() {
			automatic++
			total += item.SizeBytes
			continue
		}
		if err := s.remove(item.Name); err != nil {
			return err
		}
	}
	return nil
}

func (s *Service) remove(name string) error {
	path := filepath.Join(s.dir, name)
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return os.ErrNotExist
	}
	if err := os.Remove(path); err != nil {
		return err
	}
	err = os.Remove(path + metadataSuffix)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

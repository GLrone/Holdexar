package backup

import (
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strings"
)

type Checks struct {
	Hash       *bool `json:"hashOk"`
	Restorable *bool `json:"restorableOk"`
	Deep       *bool `json:"deepOk"`
}

type Verification struct {
	Name        string `json:"name"`
	SizeBytes   int64  `json:"sizeBytes"`
	IntegrityOK bool   `json:"integrityOk"`
	metadata
	Checks Checks `json:"checks"`
}

func (s *Service) open(name string) (*os.File, error) {
	if !backupName(name) {
		return nil, ErrName
	}
	root, err := os.OpenRoot(s.dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	info, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, os.ErrNotExist
	}
	return root.Open(name)
}

func (s *Service) Verify(ctx context.Context, name string, deep bool) (Verification, error) {
	result := Verification{Name: name}
	file, err := s.open(name)
	if errors.Is(err, os.ErrNotExist) {
		result.Checks.Hash, result.Checks.Restorable = new(false), new(false)
		return result, nil
	}
	if err != nil {
		return result, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return result, err
	}
	result.SizeBytes = info.Size()
	if info.Size() == 0 {
		result.Checks.Hash, result.Checks.Restorable = new(false), new(false)
		return result, nil
	}
	if !strings.HasSuffix(name, ".gz") {
		result.IntegrityOK, result.Games, err = inspectDatabase(ctx, file.Name(), deep)
		result.Checks.Deep = &result.IntegrityOK
		if ctx.Err() != nil {
			return result, ctx.Err()
		}
		return result, nil
	}
	if root, openErr := os.OpenRoot(s.dir); openErr == nil {
		if meta, openErr := root.Open(name + metadataSuffix); openErr == nil {
			var stored metadata
			if json.NewDecoder(io.LimitReader(meta, 8192)).Decode(&stored) == nil {
				result.metadata = stored
			}
			meta.Close()
		}
		root.Close()
	}
	if expected := result.SHA256; expected != "" {
		hash := sha256.New()
		if _, err := io.Copy(hash, contextReader{ctx, file}); err != nil {
			return result, err
		}
		result.SHA256 = hex.EncodeToString(hash.Sum(nil))
		hashOK := strings.EqualFold(expected, result.SHA256)
		result.Checks.Hash = &hashOK
		if !hashOK {
			result.Games = 0
			return result, nil
		}
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			return result, err
		}
	}
	reader, err := gzip.NewReader(file)
	if err != nil {
		result.markVerification(false, deep)
		return result, nil
	}
	defer reader.Close()
	var output io.Writer = io.Discard
	var probe *os.File
	if deep {
		probe, err = os.CreateTemp(s.dir, ".verify-*.tmp")
		if err != nil {
			return result, err
		}
		defer os.Remove(probe.Name())
		output = probe
	}
	_, err = io.Copy(output, contextReader{ctx, reader})
	if probe != nil {
		err = errors.Join(err, probe.Close())
	}
	if ctx.Err() != nil {
		return result, ctx.Err()
	}
	ok := err == nil
	if deep {
		result.Games = 0
		if ok {
			ok, result.Games, err = inspectDatabase(ctx, probe.Name(), true)
			if ctx.Err() != nil {
				return result, ctx.Err()
			}
		}
	}
	result.markVerification(ok && err == nil, deep)
	return result, nil
}

func (v *Verification) markVerification(ok, deep bool) {
	v.IntegrityOK = ok
	if deep {
		v.Checks.Deep = &ok
	} else {
		v.Checks.Restorable = &ok
	}
}

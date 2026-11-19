package updater

import (
	"archive/zip"
	"io"
)

type zipWriter struct {
	writer *zip.Writer
}

func newZipWriter(file io.Writer) *zipWriter {
	return &zipWriter{writer: zip.NewWriter(file)}
}

func (z *zipWriter) add(name string, content []byte) error {
	entry, err := z.writer.Create(name)
	if err != nil {
		return err
	}
	_, err = entry.Write(content)
	return err
}

func (z *zipWriter) close() error {
	return z.writer.Close()
}

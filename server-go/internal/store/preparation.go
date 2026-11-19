package store

import (
	"context"
	"database/sql"

	"holdexar/hostkit"
)

const CurrentSchemaVersion = 34

func LockDatabaseForPreparation(ctx context.Context, path string) (func(), error) {
	file, err := lockFile(hostkit.ProcessWriteLockPath(path), defaultLockTimeout, ctx)
	if err != nil {
		return nil, err
	}
	return func() { unlockFile(file) }, nil
}

func OpenReadOnly(path string) (*sql.DB, error) {
	return openReadOnly(path)
}

func CurrentSchemaSQL() string {
	return bootstrapSchema
}

func CurrentSeedSQL() string {
	return bootstrapSeeds
}

package config

import "sync"

// Snapshot is an immutable pair of compiled configuration and its source.
type Snapshot struct {
	Config *Config
	Source string
}

// Manager atomically publishes the active functional configuration.
type Manager struct {
	mu     sync.RWMutex
	active *Snapshot
}

func NewManager(cfg *Config, source []byte) *Manager {
	manager := &Manager{}
	manager.Activate(cfg, source)
	return manager
}

func (m *Manager) Current() *Snapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.active
}

func (m *Manager) Activate(cfg *Config, source []byte) {
	m.mu.Lock()
	m.active = &Snapshot{Config: cfg, Source: string(source)}
	m.mu.Unlock()
}

func (m *Manager) Read(fn func(*Snapshot) error) error {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return fn(m.active)
}

func (m *Manager) Update(fn func(*Snapshot) (*Snapshot, error)) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	next, err := fn(m.active)
	if err != nil {
		return err
	}
	m.active = next
	return nil
}

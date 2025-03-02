package sas_helpers_test

import (
	"testing"

	"github.com/stretchr/testify/assert"

	"magma/dp/cloud/go/active_mode_controller/internal/message_generator/sas"
	"magma/dp/cloud/go/active_mode_controller/internal/message_generator/sas_helpers"
)

const request = `{"cbsdId":"someId"}`

func TestFilter(t *testing.T) {
	data := []struct {
		name     string
		pending  []string
		expected []*sas.Request
	}{
		{
			name:     "Should filter request if pending",
			pending:  []string{request},
			expected: []*sas.Request{},
		},
		{
			name:     "Should not filter request if not pending",
			pending:  nil,
			expected: []*sas.Request{{Data: []byte(request)}},
		},
	}
	for _, tt := range data {
		t.Run(tt.name, func(t *testing.T) {
			requests := []*sas.Request{{Data: []byte(request)}}
			actual := sas_helpers.Filter(tt.pending, requests)
			assert.Equal(t, tt.expected, actual)
		})
	}
}

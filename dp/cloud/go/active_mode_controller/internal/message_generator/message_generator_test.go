package message_generator_test

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/wrapperspb"

	"magma/dp/cloud/go/active_mode_controller/internal/message_generator"
	"magma/dp/cloud/go/active_mode_controller/protos/active_mode"
	"magma/dp/cloud/go/active_mode_controller/protos/requests"
)

func TestGenerateMessages(t *testing.T) {
	const timeout = 100 * time.Second
	now := time.Unix(1000, 0)
	data := []struct {
		name             string
		state            *active_mode.State
		expectedRequests []*requests.RequestPayload
		expectedActions  []*active_mode.DeleteCbsdRequest
	}{
		{
			name: "Should do nothing for unregistered non active cbsd",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Unregistered,
					Cbsd: &active_mode.Cbsd{
						State:             active_mode.CbsdState_Unregistered,
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
		},
		{
			name: "Should do nothing when inactive cbsd has no grants",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						State:             active_mode.CbsdState_Unregistered,
						LastSeenTimestamp: 0,
					},
				}},
			},
		},
		{
			name: "Should generate deregistration request for non active registered cbsd",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Unregistered,
					Cbsd: &active_mode.Cbsd{
						Id:                "some_cbsd_id",
						State:             active_mode.CbsdState_Registered,
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{{
				Payload: `{
	"deregistrationRequest": [
		{
			"cbsdId": "some_cbsd_id"
		}
	]
}`,
			}},
		},
		{
			name: "Should generate registration request for active non registered cbsd",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						UserId:            "some_user_id",
						FccId:             "some_fcc_id",
						SerialNumber:      "some_serial_number",
						State:             active_mode.CbsdState_Unregistered,
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{{
				Payload: `{
	"registrationRequest": [
		{
			"userId": "some_user_id",
			"fccId": "some_fcc_id",
			"cbsdSerialNumber": "some_serial_number"
		}
]
}`,
			}},
		},
		{
			name: "Should generate spectrum inquiry request when there are no available channels",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:                "some_cbsd_id",
						State:             active_mode.CbsdState_Registered,
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: getSpectrumInquiryRequest(),
		},
		{
			name: "Should generate spectrum inquiry request when all channels are unsuitable",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:    "some_cbsd_id",
						State: active_mode.CbsdState_Registered,
						Channels: []*active_mode.Channel{{
							FrequencyRange: &active_mode.FrequencyRange{
								Low:  3.62e9,
								High: 3.63e9,
							},
							MaxEirp: wrapperspb.Float(4),
						}},
						EirpCapabilities: &active_mode.EirpCapabilities{
							MinPower:      0,
							MaxPower:      10,
							AntennaGain:   15,
							NumberOfPorts: 1,
						},
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: getSpectrumInquiryRequest(),
		},
		{
			name: "Should generate grant request when there are channels",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:    "some_cbsd_id",
						State: active_mode.CbsdState_Registered,
						Channels: []*active_mode.Channel{{
							FrequencyRange: &active_mode.FrequencyRange{
								Low:  3.62e9,
								High: 3.63e9,
							},
							MaxEirp: wrapperspb.Float(15),
						}},
						EirpCapabilities: &active_mode.EirpCapabilities{
							MinPower:      0,
							MaxPower:      100,
							AntennaGain:   0,
							NumberOfPorts: 1,
						},
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{{
				Payload: `{
	"grantRequest": [
		{
			"cbsdId": "some_cbsd_id",
			"operationParam": {
				"maxEirp": 15,
				"operationFrequencyRange": {
					"lowFrequency": 3620000000,
					"highFrequency": 3630000000
				}
			}
		}
	]
}`,
			}},
		},
		{
			name: "Should send heartbeat message for grant in granted state",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:    "some_cbsd_id",
						State: active_mode.CbsdState_Registered,
						Grants: []*active_mode.Grant{{
							Id:    "some_grant_id",
							State: active_mode.GrantState_Granted,
						}},
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{{
				Payload: `{
	"heartbeatRequest": [
		{
			"cbsdId": "some_cbsd_id",
			"grantId": "some_grant_id",
			"operationState": "GRANTED"
		}
	]
}`,
			}},
		},
		{
			name: "Should send both heartbeat and relinquish message for 2 grants when one is in Granted state and the other in Unsync",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:    "some_cbsd_id",
						State: active_mode.CbsdState_Registered,
						Grants: []*active_mode.Grant{
							{
								Id:    "some_grant_id",
								State: active_mode.GrantState_Granted,
							},
							{
								Id:    "some_other_grant_id",
								State: active_mode.GrantState_Unsync,
							},
						},
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{
				{
					Payload: `{
	"heartbeatRequest": [
		{
			"cbsdId": "some_cbsd_id",
			"grantId": "some_grant_id",
			"operationState": "GRANTED"
		}
	]
}`,
				},
				{
					Payload: `{
	"relinquishmentRequest": [
		{
			"cbsdId": "some_cbsd_id",
			"grantId": "some_other_grant_id"
		}
	]
}`,
				},
			},
		},
		{
			name: "Should send relinquish message when inactive for too long",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:    "some_cbsd_id",
						State: active_mode.CbsdState_Registered,
						Grants: []*active_mode.Grant{{
							Id:    "some_grant_id",
							State: active_mode.GrantState_Granted,
						}},
						LastSeenTimestamp: 0,
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{{
				Payload: `{
	"relinquishmentRequest": [
		{
			"cbsdId": "some_cbsd_id",
			"grantId": "some_grant_id"
		}
	]
}`,
			}},
		},
		{
			name: "Should deregister deleted cbsd",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						Id:                "some_cbsd_id",
						State:             active_mode.CbsdState_Registered,
						IsDeleted:         true,
						LastSeenTimestamp: now.Unix(),
					},
				}},
			},
			expectedRequests: []*requests.RequestPayload{{
				Payload: `{
	"deregistrationRequest": [
		{
			"cbsdId": "some_cbsd_id"
		}
	]
}`,
			}},
		},
		{
			name: "Should delete unregistered cbsd marked as deleted",
			state: &active_mode.State{
				ActiveModeConfigs: []*active_mode.ActiveModeConfig{{
					DesiredState: active_mode.CbsdState_Registered,
					Cbsd: &active_mode.Cbsd{
						SerialNumber:      "some_serial_number",
						State:             active_mode.CbsdState_Unregistered,
						LastSeenTimestamp: now.Unix(),
						IsDeleted:         true,
					},
				}},
			},
			expectedActions: []*active_mode.DeleteCbsdRequest{{
				SerialNumber: "some_serial_number",
			}},
		},
	}
	for _, tt := range data {
		t.Run(tt.name, func(t *testing.T) {
			g := message_generator.NewMessageGenerator(0, timeout)
			msgs := g.GenerateMessages(tt.state, now)
			p := &stubProvider{}
			for _, msg := range msgs {
				_ = msg.Send(context.Background(), p)
			}
			require.Len(t, p.requests, len(tt.expectedRequests))
			for i := range tt.expectedRequests {
				assert.JSONEq(t, tt.expectedRequests[i].Payload, p.requests[i].Payload)
			}
			require.Len(t, p.actions, len(tt.expectedActions))
			for i := range tt.expectedActions {
				assert.Equal(t, tt.expectedActions[i], p.actions[i])
			}
		})
	}
}

func getSpectrumInquiryRequest() []*requests.RequestPayload {
	return []*requests.RequestPayload{{
		Payload: `{
	"spectrumInquiryRequest": [
		{
			"cbsdId": "some_cbsd_id",
			"inquiredSpectrum": [
				{
					"lowFrequency": 3550000000,
					"highFrequency": 3700000000
				}
			]
		}
	]
}`,
	}}
}

type stubProvider struct {
	requests []*requests.RequestPayload
	actions  []*active_mode.DeleteCbsdRequest
}

func (s *stubProvider) GetRequestsClient() requests.RadioControllerClient {
	return &stubRadioControllerClient{requests: &s.requests}
}

func (s *stubProvider) GetActiveModeClient() active_mode.ActiveModeControllerClient {
	return &stubActiveModeControllerClient{actions: &s.actions}
}

type stubActiveModeControllerClient struct {
	actions *[]*active_mode.DeleteCbsdRequest
}

func (s *stubActiveModeControllerClient) GetState(_ context.Context, _ *active_mode.GetStateRequest, _ ...grpc.CallOption) (*active_mode.State, error) {
	panic("not implemented")
}

func (s *stubActiveModeControllerClient) DeleteCbsd(_ context.Context, in *active_mode.DeleteCbsdRequest, _ ...grpc.CallOption) (*active_mode.DeleteCbsdResponse, error) {
	*s.actions = append(*s.actions, in)
	return nil, nil
}

type stubRadioControllerClient struct {
	requests *[]*requests.RequestPayload
}

func (s *stubRadioControllerClient) UploadRequests(_ context.Context, in *requests.RequestPayload, _ ...grpc.CallOption) (*requests.RequestDbIds, error) {
	*s.requests = append(*s.requests, in)
	return nil, nil
}

func (s *stubRadioControllerClient) GetResponse(_ context.Context, _ *requests.RequestDbId, _ ...grpc.CallOption) (*requests.ResponsePayload, error) {
	panic("not implemented")
}

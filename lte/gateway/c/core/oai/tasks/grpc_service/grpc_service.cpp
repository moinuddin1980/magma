/*
 * Licensed to the OpenAirInterface (OAI) Software Alliance under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The OpenAirInterface Software Alliance licenses this file to You under
 * the terms found in the LICENSE file in the root of this source tree.
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *-------------------------------------------------------------------------------
 * For more information about the OpenAirInterface (OAI) Software Alliance:
 *      contact@openairinterface.org
 */
#include "lte/gateway/c/core/oai/include/grpc_service.h"

#include <grpcpp/grpcpp.h>
#include <grpcpp/security/server_credentials.h>
#include <memory>

#include "lte/gateway/c/core/oai/tasks/grpc_service/CSFBGatewayServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/SMSOrc8rGatewayServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/S1apServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/S6aServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/SpgwServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/AmfServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/HaServiceImpl.h"
#include "lte/gateway/c/core/oai/tasks/grpc_service/S8ServiceImpl.h"

extern "C" {
#include "lte/gateway/c/core/oai/common/log.h"
#include "lte/gateway/c/core/oai/include/mme_config.h"
}

using grpc::InsecureServerCredentials;
using grpc::Server;
using grpc::ServerBuilder;
using magma::AmfServiceImpl;
using magma::CSFBGatewayServiceImpl;
using magma::HaServiceImpl;
using magma::S1apServiceImpl;
using magma::S6aServiceImpl;
using magma::S8ServiceImpl;
using magma::SMSOrc8rGatewayServiceImpl;
using magma::SpgwServiceImpl;

static AmfServiceImpl amf_service;
static S6aServiceImpl s6a_service;
static CSFBGatewayServiceImpl sgs_service;
static SMSOrc8rGatewayServiceImpl sms_orc8r_service;
static S1apServiceImpl s1ap_service;
static HaServiceImpl ha_service;
#if EMBEDDED_SGW
static SpgwServiceImpl spgw_service;
static S8ServiceImpl s8_service;
#endif
static std::unique_ptr<Server> server;

// TODO Candidate: GRPC service may be evolved into a
// MagmaService, which implements Service303::Service as the
// base service and can add other services on top.
void start_grpc_service(bstring server_address) {
  OAILOG_INFO(LOG_SPGW_APP, "Starting service at : %s\n ",
              bdata(server_address));
  ServerBuilder builder;
  builder.AddListeningPort(bdata(server_address),
                           grpc::InsecureServerCredentials());
  builder.RegisterService(&amf_service);
  builder.RegisterService(&s6a_service);
  // Start the SGS service only if non_eps_service_control is not set to OFF
  char* non_eps_service_control = bdata(mme_config.non_eps_service_control);
  if (non_eps_service_control) {
    if (!strcmp(non_eps_service_control, "CSFB_SMS") ||
        !strcmp(non_eps_service_control, "SMS")) {
      builder.RegisterService(&sgs_service);
    }
    // Start the SMS service only if non_eps_service_control is set to SMS_ORC8R
    if (!strcmp(non_eps_service_control, "SMS_ORC8R")) {
      builder.RegisterService(&sms_orc8r_service);
    }
  }
  builder.RegisterService(&s1ap_service);
  if (mme_config.use_ha) {
    builder.RegisterService(&ha_service);
  }

#if EMBEDDED_SGW
  builder.RegisterService(&spgw_service);
  builder.RegisterService(&s8_service);
#endif
  server = builder.BuildAndStart();
}

void stop_grpc_service(void) {
  server->Shutdown();
  server->Wait();  // Blocks until server finishes shutting down
}

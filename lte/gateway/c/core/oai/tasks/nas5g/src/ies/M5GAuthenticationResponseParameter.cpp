/*
   Copyright 2020 The Magma Authors.
   This source code is licensed under the BSD-style license found in the
   LICENSE file in the root directory of this source tree.
   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
 */

#include <sstream>
#include <string.h>
#include <cstring>
#include <cstdint>
#include "lte/gateway/c/core/oai/tasks/nas5g/include/ies/M5GAuthenticationResponseParameter.h"
#include "lte/gateway/c/core/oai/tasks/nas5g/include/M5GCommonDefs.h"

namespace magma5g {
AuthenticationResponseParameterMsg::AuthenticationResponseParameterMsg(){};
AuthenticationResponseParameterMsg::~AuthenticationResponseParameterMsg(){};

// Decode AuthenticationResponseParameter IE
int AuthenticationResponseParameterMsg::
    DecodeAuthenticationResponseParameterMsg(
        AuthenticationResponseParameterMsg* response_parameter, uint8_t iei,
        uint8_t* buffer, uint32_t len) {
  uint32_t decoded = 0;

  MLOG(MDEBUG) << "Decoding Authentication Response Parameter IE";

  if (iei > 0) {
    CHECK_IEI_DECODER(iei, *buffer);
    response_parameter->iei = *(buffer + decoded);
    MLOG(MDEBUG) << " ElementID : " << std::hex << int(response_parameter->iei);
    decoded++;
  }
  response_parameter->length = *(buffer + decoded);
  MLOG(MDEBUG) << " Length : " << std::dec << int(response_parameter->length);
  decoded++;
  response_parameter->response_parameter[0] = 0;
  for (int i = 0; i < (int)(response_parameter->length); i++) {
    response_parameter->response_parameter[i] = *(buffer + decoded);
    decoded++;
  }
  for (int i = 0; i < (int)(response_parameter->length); i++) {
    MLOG(MDEBUG) << " RES : " << std::hex
                 << int(response_parameter->response_parameter[i]);
  }
  return (decoded);
};

// Encode AuthenticationResponseParameter IE
int AuthenticationResponseParameterMsg::
    EncodeAuthenticationResponseParameterMsg(
        AuthenticationResponseParameterMsg* response_parameter, uint8_t iei,
        uint8_t* buffer, uint32_t len) {
  uint32_t encoded = 0;
#ifdef HANDLE_POST_MVC
  // Checking IEI and pointer
  CHECK_PDU_POINTER_AND_LENGTH_ENCODER(
      buffer, AUTHENTICATION_RESPONSE_PARAMETER_MIN_LEN, len);

  if (iei > 0) {
    CHECK_IEI_ENCODER((unsigned char)iei, response_parameter->iei);
    *buffer = iei;
    MLOG(MDEBUG) << "In EncodeAuthenticationResponseParameterMsg: iei"
                 << std::hex << int(*buffer) << std::endl;
    encoded++;
  } else {
    return 0;
  }

  lenPtr = (uint16_t*)(buffer + encoded);
  encoded++;
  std::copy(response_parameter->response_parameter.begin(),
            response_parameter->response_parameter.end(), buffer + encoded);
  BUFFER_PRINT_LOG(buffer + encoded,
                   response_parameter->response_parameter.length());
  encoded = encoded + response_parameter->response_parameter.length();
  *lenPtr = encoded - 1 - ((iei > 0) ? 1 : 0);
#endif
  return (encoded);
};
}  // namespace magma5g

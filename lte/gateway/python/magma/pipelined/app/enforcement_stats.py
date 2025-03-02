"""
Copyright 2020 The Magma Authors.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import ipaddress
import os
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta
from subprocess import check_output

import ryu.app.ofctl.api as ofctl_api
from lte.protos.pipelined_pb2 import RuleModResult
from lte.protos.policydb_pb2 import FlowDescription
from lte.protos.session_manager_pb2 import (
    RuleRecord,
    RuleRecordTable,
    UPFSessionState,
)
from magma.pipelined.app.base import (
    ControllerType,
    MagmaController,
    global_epoch,
)
from magma.pipelined.app.policy_mixin import (
    DROP_FLOW_STATS,
    IGNORE_STATS,
    PROCESS_STATS,
    PolicyMixin,
)
from magma.pipelined.app.restart_mixin import DefaultMsgsMap, RestartMixin
from magma.pipelined.imsi import decode_imsi, encode_imsi
from magma.pipelined.ipv6_prefix_store import get_ipv6_prefix
from magma.pipelined.ng_manager.session_state_manager import SessionStateManager
from magma.pipelined.openflow import flows
from magma.pipelined.openflow.exceptions import (
    MagmaDPDisconnectedError,
    MagmaOFError,
)
from magma.pipelined.openflow.magma_match import MagmaMatch
from magma.pipelined.openflow.messages import MessageHub, MsgChannel
from magma.pipelined.openflow.registers import (
    DIRECTION_REG,
    IMSI_REG,
    NG_SESSION_ID_REG,
    REG_ZERO_VAL,
    RULE_NUM_REG,
    RULE_VERSION_REG,
    SCRATCH_REGS,
    Direction,
)
from magma.pipelined.policy_converters import (
    convert_ipv4_str_to_ip_proto,
    convert_ipv6_str_to_ip_proto,
    get_eth_type,
    get_ue_ip_match_args,
)
from magma.pipelined.utils import Utils
from ryu.app.ofctl.exception import (
    InvalidDatapath,
    OFError,
    UnexpectedMultiReply,
)
from ryu.controller import dpset, ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto.ofproto_v1_4 import OFPMPF_REPLY_MORE

ETH_FRAME_SIZE_BYTES = 14


class EnforcementStatsController(PolicyMixin, RestartMixin, MagmaController):
    """
    This openflow controller installs flows for aggregating policy usage
    statistics, which are sent to sessiond for tracking.

    It periodically polls OVS for flow stats on the its table and reports the
    usage records to session manager via RPC. Flows are deleted when their
    version (reg4 match) is different from the current version of the rule for
    the subscriber maintained by the rule version mapper.
    """

    APP_NAME = 'enforcement_stats'
    APP_TYPE = ControllerType.LOGICAL
    SESSIOND_RPC_TIMEOUT = 10
    # 0xffffffffffffffff is reserved in openflow
    DEFAULT_FLOW_COOKIE = 0xfffffffffffffffe
    INIT_SLEEP_TIME = 3
    MAX_DELAY_INTERVALS = 20

    ng_config = namedtuple(
            'ng_config',
            ['ng_service_enabled', 'sessiond_setinterface'],
    )
    _CONTEXTS = {
        'dpset': dpset.DPSet,
    }

    def __init__(self, *args, **kwargs):
        super(EnforcementStatsController, self).__init__(*args, **kwargs)
        self.tbl_num = self._service_manager.get_table_num(self.APP_NAME)
        self.next_table = \
            self._service_manager.get_next_table_num(self.APP_NAME)
        self.dpset = kwargs['dpset']
        self.loop = kwargs['loop']
        # Spawn a thread to poll for flow stats
        poll_interval = kwargs['config']['enforcement']['poll_interval']
        # Create a rpc channel to sessiond
        self.sessiond = kwargs['rpc_stubs']['sessiond']
        self._msg_hub = MessageHub(self.logger)
        self.unhandled_stats_msgs = []  # Store multi-part responses from ovs
        self.total_usage = {}  # Store total usage
        self._clean_restart = kwargs['config']['clean_restart']
        self._redis_enabled = kwargs['config'].get('redis_enabled', False)
        self._unmatched_bytes = 0  # Store bytes matched by default rule if any
        self._default_drop_flow_name = \
            kwargs['config']['enforcement']['default_drop_flow_name']
        self.flow_stats_thread = hub.spawn(self._monitor, poll_interval)
        self._print_grpc_payload = os.environ.get('MAGMA_PRINT_GRPC_PAYLOAD')
        self._last_poll_time = datetime.now()
        self._last_report_timestamp = datetime.now()
        self._bridge_name = kwargs['config']['bridge_name']
        self._periodic_stats_reporting = kwargs['config']['enforcement'].get('periodic_stats_reporting', True)
        if self._print_grpc_payload is None:
            self._print_grpc_payload = \
                kwargs['config'].get('magma_print_grpc_payload', False)
        self._restart_info_store = kwargs['restart_info_store']
        self._ovs_restarted = self._was_ovs_restarted()
        self.ng_config = self._get_ng_config(kwargs['config'], kwargs['rpc_stubs'])
        self._prefix_mapper = kwargs['interface_to_prefix_mapper']

    def _get_ng_config(self, config_dict, rpc_stub_dict):
        ng_service_enabled = config_dict.get('enable5g_features', None)

        sessiond_setinterface = rpc_stub_dict.get('sessiond_setinterface')

        return self.ng_config(ng_service_enabled=ng_service_enabled, sessiond_setinterface=sessiond_setinterface)

    def delete_all_flows(self, datapath):
        flows.delete_all_flows_from_table(datapath, self.tbl_num)

    def cleanup_state(self):
        """
        When we remove/reinsert flows we need to remove old usage maps as new
        flows will have reset stat counters
        """
        self.unhandled_stats_msgs = []
        self.total_usage = {}
        self._unmatched_bytes = 0

    def initialize_on_connect(self, datapath):
        """
        Install the default flows on datapath connect event.

        Args:
            datapath: ryu datapath struct
        """
        self._datapath = datapath

    def _get_default_flow_msgs(self, datapath) -> DefaultMsgsMap:
        """
        Gets the default flow msg that drops traffic

        Args:
            datapath: ryu datapath struct
        Returns:
            The list of default msgs to add
        """
        match = MagmaMatch()
        msg = flows.get_add_drop_flow_msg(
            datapath, self.tbl_num, match,
            priority=flows.MINIMUM_PRIORITY,
            cookie=self.DEFAULT_FLOW_COOKIE,
        )

        return {self.tbl_num: [msg]}

    def cleanup_on_disconnect(self, datapath):
        """
        Cleanup flows on datapath disconnect event.

        Args:
            datapath: ryu datapath struct
        """
        if self._clean_restart:
            self.delete_all_flows(datapath)

    def _install_flow_for_rule(
        self, imsi, msisdn: bytes, uplink_tunnel: int, ip_addr, apn_ambr, rule, version, shard_id,
        local_f_teid_ng: int,
    ):
        """
        Install a flow to get stats for a particular rule. Flows will match on
        IMSI, cookie (the rule num), in/out direction

        Args:
            imsi (string): subscriber to install rule for
            msisdn (bytes): subscriber MSISDN
            uplink_tunnel (int): tunnel ID of the subscriber.
            ip_addr (string): subscriber session ipv4 address
            rule (PolicyRule): policy rule proto
        """
        def fail(err):
            self.logger.error(
                "Failed to install rule %s for subscriber %s: %s",
                rule.id, imsi, err,
            )
            return RuleModResult.FAILURE

        msgs = self._get_rule_match_flow_msgs(
            imsi, msisdn, uplink_tunnel,
            ip_addr, apn_ambr, rule, version, shard_id,
            local_f_teid_ng,
        )
        try:
            chan = self._msg_hub.send(msgs, self._datapath)
        except MagmaDPDisconnectedError:
            self.logger.error(
                "Datapath disconnected, failed to install rule %s"
                "for imsi %s", rule, imsi,
            )
            return RuleModResult.FAILURE
        for _ in range(len(msgs)):
            try:
                result = chan.get()
            except MsgChannel.Timeout:
                return fail("No response from OVS")
            if not result.ok():
                return fail(result.exception())

        return RuleModResult.SUCCESS

    @set_ev_cls(ofp_event.EventOFPBarrierReply, MAIN_DISPATCHER)
    def _handle_barrier(self, ev):
        self._msg_hub.handle_barrier(ev)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def _handle_error(self, ev):
        self._msg_hub.handle_error(ev)

    # pylint: disable=protected-access,unused-argument
    def _get_rule_match_flow_msgs(self, imsi, _, __, ip_addr, ambr, rule, version, shard_id, local_f_teid_ng):
        """
        Returns flow add messages used for rule matching.
        """
        rule_num = self._rule_mapper.get_or_create_rule_num(rule.id)
        self.logger.debug(
            'Installing flow for %s with rule num %s (version %s)', imsi,
            rule_num, version,
        )
        inbound_rule_match = _generate_rule_match(
            imsi, ip_addr, rule_num,
            version, Direction.IN,
            local_f_teid_ng,
        )
        outbound_rule_match = _generate_rule_match(
            imsi, ip_addr, rule_num,
            version, Direction.OUT,
            local_f_teid_ng,
        )

        flow_actions = [flow.action for flow in rule.flow_list]
        msgs = []
        if FlowDescription.PERMIT in flow_actions:
            inbound_rule_match._match_kwargs[SCRATCH_REGS[1]] = PROCESS_STATS
            outbound_rule_match._match_kwargs[SCRATCH_REGS[1]] = PROCESS_STATS
            msgs.extend([
                flows.get_add_drop_flow_msg(
                    self._datapath,
                    self.tbl_num,
                    inbound_rule_match,
                    priority=flows.DEFAULT_PRIORITY,
                    cookie=shard_id,
                ),
                flows.get_add_drop_flow_msg(
                    self._datapath,
                    self.tbl_num,
                    outbound_rule_match,
                    priority=flows.DEFAULT_PRIORITY,
                    cookie=shard_id,
                ),
            ])
        else:
            inbound_rule_match._match_kwargs[SCRATCH_REGS[1]] = DROP_FLOW_STATS
            outbound_rule_match._match_kwargs[SCRATCH_REGS[1]] = DROP_FLOW_STATS
            msgs.extend([
                flows.get_add_drop_flow_msg(
                    self._datapath,
                    self.tbl_num,
                    inbound_rule_match,
                    priority=flows.DEFAULT_PRIORITY,
                    cookie=shard_id,
                ),
                flows.get_add_drop_flow_msg(
                    self._datapath,
                    self.tbl_num,
                    outbound_rule_match,
                    priority=flows.DEFAULT_PRIORITY,
                    cookie=shard_id,
                ),
            ])

        if rule.app_name:
            inbound_rule_match._match_kwargs[SCRATCH_REGS[1]] = IGNORE_STATS
            outbound_rule_match._match_kwargs[SCRATCH_REGS[1]] = IGNORE_STATS
            msgs.extend([
                flows.get_add_drop_flow_msg(
                    self._datapath,
                    self.tbl_num,
                    inbound_rule_match,
                    priority=flows.DEFAULT_PRIORITY,
                    cookie=shard_id,
                ),
                flows.get_add_drop_flow_msg(
                    self._datapath,
                    self.tbl_num,
                    outbound_rule_match,
                    priority=flows.DEFAULT_PRIORITY,
                    cookie=shard_id,
                ),
            ])
        return msgs

    def _get_default_flow_msgs_for_subscriber(self, imsi, ip_addr, local_f_teid_ng):
        match_in = _generate_rule_match(
            imsi, ip_addr, 0, 0,
            Direction.IN, local_f_teid_ng,
        )
        match_out = _generate_rule_match(
            imsi, ip_addr, 0, 0,
            Direction.OUT, local_f_teid_ng,
        )

        return [
            flows.get_add_drop_flow_msg(
                self._datapath, self.tbl_num, match_in,
                priority=Utils.DROP_PRIORITY,
            ),
            flows.get_add_drop_flow_msg(
                self._datapath, self.tbl_num, match_out,
                priority=Utils.DROP_PRIORITY,
            ),
        ]

    def _install_redirect_flow(self, imsi, ip_addr, rule, version):
        pass

    def _install_default_flow_for_subscriber(self, imsi, ip_addr, local_f_teid_ng):
        """
        Add a low priority flow to drop a subscriber's traffic.

        Args:
            imsi (string): subscriber id
            ip_addr (string): subscriber ip_addr
        """
        msgs = self._get_default_flow_msgs_for_subscriber(imsi, ip_addr, local_f_teid_ng)
        if msgs:
            chan = self._msg_hub.send(msgs, self._datapath)
            self._wait_for_responses(chan, len(msgs))

    def get_policy_usage(self, fut):
        record_table = RuleRecordTable(
            records=self.total_usage.values(),
            epoch=global_epoch,
        )
        fut.set_result(record_table)

    def _monitor(self, poll_interval):
        """
        Main thread that sends a stats request at the configured interval in
        seconds.
        """
        while not self.init_finished:
            # Still send an empty report -> for pipelined setup
            self._report_usage({})
            hub.sleep(self.INIT_SLEEP_TIME)
        if not self._periodic_stats_reporting:
            return
        while True:
            hub.sleep(poll_interval)
            now = datetime.now()
            delta = get_adjusted_delta(self._last_report_timestamp, now)
            if delta > poll_interval * self.MAX_DELAY_INTERVALS:
                self.logger.info(
                    'Previous update missing, current time %s, last '
                    'report timestamp %s, last poll timestamp %s',
                    now.strftime("%H:%M:%S"),
                    self._last_report_timestamp.strftime("%H:%M:%S"),
                    self._last_poll_time.strftime("%H:%M:%S"),
                )
                self._last_report_timestamp = now
                hub.sleep(poll_interval / 2)
                continue
            if delta < poll_interval:
                continue
            self._last_poll_time = now
            self.logger.debug(
                'Started polling: %s',
                now.strftime("%H:%M:%S"),
            )
            self._poll_stats(self._datapath)

    def _poll_stats(self, datapath, cookie: int = 0, cookie_mask: int = 0):
        """
        Send a FlowStatsRequest message to the datapath
        Raises:
        MagmaOFError: if we can't poll datapath stats
        """
        try:
            flows.send_stats_request(
                datapath, self.tbl_num,
                cookie, cookie_mask,
            )
        except MagmaOFError as e:
            self.logger.warning("Couldn't poll datapath stats: %s", e)
        except Exception as e:  # pylint: disable=broad-except
            self.logger.warning("Couldn't poll datapath stats: %s", e)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        """
        Schedule the flow stats handling in the main event loop, so as to
        unblock the ryu event loop
        """
        if not self.init_finished:
            self.logger.debug('Setup not finished, skipping stats reply')
            return

        if self._datapath_id != ev.msg.datapath.id:
            self.logger.debug('Ignoring stats from different bridge')
            return

        self.unhandled_stats_msgs.append(ev.msg.body)
        if ev.msg.flags == OFPMPF_REPLY_MORE:
            # Wait for more multi-part responses thats received for the
            # single stats request.
            return
        self.loop.call_soon_threadsafe(
            self._handle_flow_stats, self.unhandled_stats_msgs,
        )
        self.unhandled_stats_msgs = []

    def _handle_flow_stats(self, stats_msgs):
        """
        Aggregate flow stats by rule, and report to session manager
        """
        stat_count = sum(len(flow_stats) for flow_stats in stats_msgs)
        if stat_count == 0:
            return

        self.logger.debug("Processing %s stats responses", len(stats_msgs))
        # Aggregate flows into rule records
        aggregated_msgs = []
        for flow_stats in stats_msgs:
            aggregated_msgs += flow_stats

        self.logger.debug("Processing stats of %d flows", len(aggregated_msgs))
        try:
            current_usage = self._get_usage_from_flow_stat(aggregated_msgs)
        except ConnectionError:
            self.logger.error('Failed processing stats, redis unavailable')
            self.unhandled_stats_msgs.append(stats_msgs)
            return
        # Send report even if usage is empty. Sessiond uses empty reports to
        # recognize when flows have ended
        self._report_usage(current_usage)

        # This is done primarily for CWF integration tests, TODO rm
        self.total_usage = current_usage
        # Report only if their is no change in version
        if self.ng_config.ng_service_enabled == True:
            self._prepare_session_config_report(stats_msgs)

    def deactivate_default_flow(self, imsi, ip_addr, local_f_teid_ng=0):
        if self._datapath is None:
            self.logger.error('Datapath not initialized')
            return

        match_in = _generate_rule_match(
            imsi, ip_addr, 0, 0,
            Direction.IN, local_f_teid_ng,
        )
        match_out = _generate_rule_match(
            imsi, ip_addr, 0, 0,
            Direction.OUT, local_f_teid_ng,
        )

        flows.delete_flow(self._datapath, self.tbl_num, match_in)
        flows.delete_flow(self._datapath, self.tbl_num, match_out)

    def _report_usage(self, usage):
        """
        Report usage to sessiond using rpc
        """
        record_table = RuleRecordTable(
            records=usage.values(),
            epoch=global_epoch,
            update_rule_versions=self._ovs_restarted,
        )
        if self._print_grpc_payload:
            record_msg = 'Sending RPC payload: {0}{{\n{1}}}'.format(
                record_table.DESCRIPTOR.name, str(record_table),
            )
            self.logger.info(record_msg)
        future = self.sessiond.ReportRuleStats.future(
            record_table, self.SESSIOND_RPC_TIMEOUT,
        )
        future.add_done_callback(
            lambda future: self.loop.call_soon_threadsafe(
                self._report_usage_done, future, usage.values(),
            ),
        )

    def _report_usage_done(self, future, records):
        """
        Callback after sessiond RPC completion
        """
        self._last_report_timestamp = datetime.now()
        self.logger.debug(
            'Finished reporting: %s',
            self._last_report_timestamp.strftime("%H:%M:%S"),
        )
        err = future.exception()
        if err:
            self.logger.error('Couldnt send flow records to sessiond: %s', err)
            return
        try:
            self._delete_old_flows(records)
        except ConnectionError:
            self.logger.error('Failed remove old flows, redis unavailable')
            return

    def _get_usage_from_flow_stat(self, flow_stats):
        """
        Update the rule record map with the flow stat and return the
        updated map.
        """
        current_usage = defaultdict(RuleRecord)
        for flow_stat in flow_stats:
            if flow_stat.table_id != self.tbl_num:
                # this update is not intended for policy
                continue
            rule_id = self._get_rule_id(flow_stat)
            # Rule not found, must be default flow
            if rule_id == "":
                default_flow_matched = \
                    flow_stat.cookie == self.DEFAULT_FLOW_COOKIE
                if default_flow_matched:
                    if flow_stat.byte_count != 0 and \
                       self._unmatched_bytes != flow_stat.byte_count:
                        self.logger.debug(
                            '%s bytes total not reported.',
                            flow_stat.byte_count,
                        )
                        self._unmatched_bytes = flow_stat.byte_count
                    continue
                else:
                    # This must be the default drop flow
                    rule_id = self._default_drop_flow_name
            # If this is a pass through app name flow ignore stats
            if _get_policy_type(flow_stat.match) == IGNORE_STATS:
                continue
            sid = _get_sid(flow_stat)
            if not sid:
                continue
            ipv4_addr = _get_ipv4(flow_stat)
            ipv6_addr = self._get_ipv6(flow_stat)

            local_f_teid_ng = _get_ng_local_f_id(flow_stat)

            # use a compound key to separate flows for the same rule but for
            # different subscribers
            key = sid + "|" + rule_id

            if ipv4_addr:
                key += "|" + ipv4_addr
            elif ipv6_addr:
                key += "|" + ipv6_addr

            rule_version = _get_version(flow_stat)
            if not rule_version:
                rule_version = 0

            key += "|" + str(rule_version)

            current_usage[key].rule_id = rule_id
            current_usage[key].sid = sid

            current_usage[key].rule_version = rule_version

            if ipv4_addr:
                current_usage[key].ue_ipv4 = ipv4_addr
            elif ipv6_addr:
                current_usage[key].ue_ipv6 = ipv6_addr
            if local_f_teid_ng:
                current_usage[key].teid = local_f_teid_ng

            bytes_rx = 0
            bytes_tx = 0
            if flow_stat.match[DIRECTION_REG] == Direction.IN:
                # HACK decrement byte count for downlink packets by the length
                # of an ethernet frame. Only IP and below should be counted towards
                # a user's data. Uplink does this already because the GTP port is
                # an L3 port.
                bytes_rx = _get_downlink_byte_count(flow_stat)
            else:
                bytes_tx = flow_stat.byte_count

            if _get_policy_type(flow_stat.match) == PROCESS_STATS:
                current_usage[key].bytes_rx += bytes_rx
                current_usage[key].bytes_tx += bytes_tx
            else:
                current_usage[key].dropped_rx += bytes_rx
                current_usage[key].dropped_tx += bytes_tx
        return current_usage

    def _delete_old_flows(self, records):
        """
        Check if the version of any record is older than the current version.
        If so, delete the flow.
        """
        for record in records:
            ip_addr = None
            if record.ue_ipv4:
                ip_addr = convert_ipv4_str_to_ip_proto(record.ue_ipv4)
            elif record.ue_ipv6:
                ip_addr = convert_ipv6_str_to_ip_proto(record.ue_ipv6)

            current_ver = self._session_rule_version_mapper.get_version(
                    record.sid, ip_addr, record.rule_id,
            )
            local_f_teid_ng = 0
            if record.teid:
                local_f_teid_ng = record.teid

            if current_ver == record.rule_version:
                continue

            try:
                self._delete_flow(
                    record.sid, ip_addr,
                    record.rule_id, record.rule_version, local_f_teid_ng,
                )
            except MagmaOFError as e:
                self.logger.error(
                    'Failed to delete rule %s for subscriber %s ('
                    'version: %s): %s', record.rule_id,
                    record.sid, record.rule_version, e,
                )

    def _delete_flow(self, imsi, ip_addr, rule_id, rule_version, local_f_teid_ng=0):
        rule_num = self._rule_mapper.get_or_create_rule_num(rule_id)
        match_in = _generate_rule_match(
            imsi, ip_addr, rule_num, rule_version,
            Direction.IN, local_f_teid_ng,
        )
        match_out = _generate_rule_match(
            imsi, ip_addr, rule_num, rule_version,
            Direction.OUT, local_f_teid_ng,
        )
        flows.delete_flow(
            self._datapath,
            self.tbl_num,
            match_in,
        )
        flows.delete_flow(
            self._datapath,
            self.tbl_num,
            match_out,
        )

    def _was_ovs_restarted(self):
        try:
            ovs_pid = int(check_output(["pidof", "ovs-vswitchd"]).decode())
        except Exception as e:  # pylint: disable=broad-except
            self.logger.warning("Couldn't get ovs pid: %s", e)
            ovs_pid = 0
        stored_ovs_pid = self._restart_info_store["ovs-vswitchd"]
        self._restart_info_store["ovs-vswitchd"] = ovs_pid
        self.logger.info(
            "Stored ovs_pid %d, new ovs pid %d",
            stored_ovs_pid, ovs_pid,
        )
        return ovs_pid != stored_ovs_pid

    def _get_rule_id(self, flow):
        """
        Return the rule id from the rule cookie
        """
        # the default rule will have a cookie of 0
        rule_num = flow.match.get(RULE_NUM_REG, 0)
        if rule_num == 0 or rule_num == self.DEFAULT_FLOW_COOKIE:
            return ""
        try:
            return self._rule_mapper.get_rule_id(rule_num)
        except KeyError as e:
            self.logger.error(
                'Could not find rule id for num %d: %s',
                rule_num, e,
            )
            return ""

    def get_stats(self, cookie: int = 0, cookie_mask: int = 0):
        """
        Use Ryu API to send a stats request containing cookie and cookie mask, retrieve a response and
        convert to a Rule Record Table and remove old flows
        """
        if not self._datapath:
            self.logger.error("Could not initialize datapath for stats retrieval")
            return RuleRecordTable()
        parser = self._datapath.ofproto_parser
        message = parser.OFPFlowStatsRequest(
            datapath=self._datapath,
            table_id=self.tbl_num,
            cookie=cookie,
            cookie_mask=cookie_mask,
        )
        try:
            response = ofctl_api.send_msg(
                self, message, reply_cls=parser.OFPFlowStatsReply,
                reply_multi=True,
            )
            if not response:
                self.logger.error("No rule records match the specified cookie and cookie mask")
                return RuleRecordTable()

            aggregated_msgs = []
            for r in response:
                aggregated_msgs += r.body

            usage = self._get_usage_from_flow_stat(aggregated_msgs)
            self.loop.call_soon_threadsafe(self._delete_old_flows, usage.values())
            record_table = RuleRecordTable(
                records=usage.values(),
                epoch=global_epoch,
            )
            return record_table
        except (InvalidDatapath, OFError, UnexpectedMultiReply):
            self.logger.error("Could not obtain rule records due to either InvalidDatapath, OFError or UnexpectedMultiReply")
            return RuleRecordTable()

    def _prepare_session_config_report(self, stats_msgs):
        session_config_dict = {}

        for flow_stats in stats_msgs:
            for stat in flow_stats:
                if stat.table_id != self.tbl_num:
                    continue

                local_f_teid_ng = _get_ng_local_f_id(stat)
                if not local_f_teid_ng or local_f_teid_ng == REG_ZERO_VAL:
                    continue
                # Already present
                if local_f_teid_ng in session_config_dict:
                    if local_f_teid_ng != session_config_dict[local_f_teid_ng].local_f_teid:
                        self.logger.error("Mismatch local TEID value. Need to investigate")

                    continue

                sid = _get_sid(stat)
                if not sid:
                    continue

                rule_version = _get_version(stat)
                if rule_version == 0:
                    continue

                session_config_dict[local_f_teid_ng] = \
                                             UPFSessionState(
                                                 subscriber_id=sid,
                                                 session_version=rule_version,
                                                 local_f_teid=local_f_teid_ng,
                                             )

        SessionStateManager.report_session_config_state(
            session_config_dict,
            self.ng_config.sessiond_setinterface,
        )

    def _get_ipv6(self, flow):
        if DIRECTION_REG not in flow.match:
            return None
        if flow.match[DIRECTION_REG] == Direction.OUT:
            ip_register = 'ipv6_src'
        else:
            ip_register = 'ipv6_dst'
        if ip_register not in flow.match:
            return None
        ipv6 = flow.match[ip_register]
        # masked value returned as tuple

        if type(ipv6) is tuple:
            ipv6_addr = ipv6[0]
        else:
            ipv6_addr = ipv6

        prefix = get_ipv6_prefix(ipv6_addr)
        interface = self._prefix_mapper.get_interface(prefix)
        if interface is None:
            return ipv6_addr
        # Rebuild UE IPv6 address from prefix map
        subnet = ipaddress.ip_address(prefix)
        host_id = ipaddress.ip_address(interface)
        ue_ip = ipaddress.ip_address(int(subnet) | int(host_id))

        self.logger.debug("recalc ue_ip: %s sub: %s host: %s", ue_ip, prefix, host_id)
        return str(ue_ip)


def _generate_rule_match(imsi, ip_addr, rule_num, version, direction, local_f_teid_ng=0):
    """
    Return a MagmaMatch that matches on the rule num and the version.
    """
    ip_match = get_ue_ip_match_args(ip_addr, direction)
    return MagmaMatch(
        imsi=encode_imsi(imsi), eth_type=get_eth_type(ip_addr),
        direction=direction, rule_num=rule_num,
        rule_version=version, local_f_teid_ng=local_f_teid_ng,
        **ip_match,
    )


def _get_sid(flow):
    if IMSI_REG not in flow.match:
        return None
    return decode_imsi(flow.match[IMSI_REG])


def _get_ipv4(flow):
    if DIRECTION_REG not in flow.match:
        return None
    if flow.match[DIRECTION_REG] == Direction.OUT:
        ip_register = 'ipv4_src'
    else:
        ip_register = 'ipv4_dst'
    if ip_register not in flow.match:
        return None
    ipv4 = flow.match[ip_register]
    # masked value returned as tuple
    if type(ipv4) is tuple:
        return ipv4[0]
    else:
        return ipv4


def _get_version(flow):
    if RULE_VERSION_REG not in flow.match:
        return None
    return flow.match[RULE_VERSION_REG]


def _get_downlink_byte_count(flow_stat):
    total_bytes = flow_stat.byte_count
    packet_count = flow_stat.packet_count
    return total_bytes - ETH_FRAME_SIZE_BYTES * packet_count


def _get_policy_type(match):
    if SCRATCH_REGS[1] not in match:
        return None
    return match[SCRATCH_REGS[1]]


def get_adjusted_delta(begin, end):
    # Add on a bit of time to compensate for grpc
    return (end - begin + timedelta(milliseconds=150)).total_seconds()


def _get_ng_local_f_id(flow):
    if NG_SESSION_ID_REG not in flow.match:
        return None
    return flow.match[NG_SESSION_ID_REG]

import argparse
import copy
import socket
import time
import warnings
from random import randrange as rnd, choice

import colors

from communicate import send_data_quite, recv_data, CorruptedMessageError, \
    get_ip_address, MIN_PORT_NUMBER, MAX_PORT_NUMBER, DEFAULT_PORT_NUMBER, \
    CONNECTION_ABORTED_ERROR_WARNING_TMPL, CONNECTION_RESET_ERROR_WARNING_TMPL


DT_SECONDS = 0.001
WINDOW_SHAPE = (800, 600)

MAX_NUM_PLAYERS = 10

MAX_NUM_CUBES = 20
DEFAULT_NUM_CUBES = 5


def get_app_args():
    parser = argparse.ArgumentParser(
        "Это скрипт для запуска сервера игры 'Cube Game'. Игра позволяет "
        "схватить мышкой один из кубиков и двигать его. В игре может "
        "участвовать до {} игроков. Чтобы играть, необходимо: (1)запустить "
        "этот скрипт, (2)запустить скрипт client.py на компьютерах каждого "
        "из игроков и передать при этом ip сервера. ip и порт сервера "
        "печатаются при запуске сервера. Если Вы играете на той же машине, "
        "на которой запущен сервер, то ip при запуске клиента можно не "
        "указывать. Программа может работать неправильно, если соединение "
        "было потеряно, а затем восстановлено. В таком случае клиента "
        "нужно перезапустить.".format(
            MAX_NUM_PLAYERS)
    )
    parser.add_argument(
        "--server_port",
        "-p",
        help="Порт сервера, через который осуществляется соединение. "
             "Разрешенные значения: {} - {}. Значение по умолчанию "
             "{}.".format(
                 MIN_PORT_NUMBER, MAX_PORT_NUMBER, DEFAULT_PORT_NUMBER),
        type=int,
        default=DEFAULT_PORT_NUMBER
    )
    parser.add_argument(
        "--num_cubes",
        "-n",
        help="Число кубиков в игре. Макчимальное число кубиков: {}. "
             "Значение по умолчанию {}.".format(
                 MAX_NUM_CUBES, DEFAULT_NUM_CUBES),
        type=int,
        default=DEFAULT_NUM_CUBES
    )
    return parser.parse_args()


class PlayerScenario:
    def __init__(self, game, player_addr):
        self.game = game
        self.player_addr = player_addr

        self.player_states = {
            'waiting_for_init': {
                "act": {
                    "game_method": self.game.init_player,
                    "change_state": self.change_state_to_grab_move,
                },
                'process_event': {
                    "game_method": self.game.warn_events_before_init,
                    "change_state": None
                }
            },
            'grab_move': {
                "act": {
                    "game_method": None,
                    "change_state": None
                },
                "event": {
                    "game_method": self.game.process_event,
                    "change_state": None
                }
            }
        }

        self.current_state = "waiting_for_init"

    def act(self):
        act_description = self.player_states[self.current_state]['act']
        game_method = act_description['game_method']
        if game_method is None:
            result = None
        else:
            result = game_method(self.player_addr)
        change_state_method = act_description['change_state']
        if change_state_method is not None:
            change_state_method(result)

    def process_event(self, addr, event):
        assert addr == self.player_addr
        process_event_description = \
            self.player_states[self.current_state]['event']
        game_method = process_event_description['game_method']
        if game_method is None:
            result = None
        else:
            result = game_method(addr, event)
        change_state_method = process_event_description['change_state']
        if change_state_method is not None:
            change_state_method(result)

    def change_state_to_grab_move(self, game_method_result):
        self.current_state = 'grab_move'


class CubeServer:
    def __init__(self, cube_canvas, id_, x, y, size, color):
        self.cube_canvas = cube_canvas
        self.id = id_
        self.x = x
        self.y = y
        self.size = size
        self.color = color

        self.grabbing_point = None

    def is_coord_missing(self, addr, event):
        missing_coords = []
        if 'x' not in event:
            missing_coords.append('x')
        if 'y' not in event:
            missing_coords.append('y')
        if missing_coords:
            warning_msg = "В описании события не хватает {}." \
                "Захват кубика не будет осуществлен.".format(
                    ' и '.join(map(repr, missing_coords))
                )
            warnings.warn(warning_msg)
            conn = self.cube_canvas.get_root().conns_to_clients[addr]
            msg = {
                'type': 'error_msg',
                'error_class': 'ValueError',
                'addr': addr,
                'msg': warning_msg,
                'event': event
            }
            send_data_quite(conn, addr, msg)
        return bool(missing_coords)

    def are_x_and_y_ok(self, addr, event):
        ok = not self.is_coord_missing(addr, event)
        if not (self.x <= event['x'] <= self.x + self.size
                and self.y <= event['y'] <= self.y + self.size):
            ok = False
            warning_msg = "У кубика с id {} разные координаты или размер " \
                "в клиентской и серверной частях программы. В результате " \
                "мышка не попадает по кубику из серверной части программы. " \
                "Захват кубика не будет осуществлен.".format(self.id)
            warnings.warn(warning_msg)
            conn = self.cube_canvas.get_root().conns_to_clients[addr]
            msg = {
                'type': 'error_msg',
                'error_class': 'ValueError',
                'addr': addr,
                'msg': warning_msg,
                'event': event,
                'cube_coords_on_server': (self.x, self.y),
                'cube_size_on_server': self.size
            }
            send_data_quite(conn, addr, msg)
        return ok

    def move_by_grabbing_point(self, addr, x, y):
        root = self.cube_canvas.get_root()
        assert self.grabbing_point is not None, "Метод " \
            "`CubeServer.move_by_grabbing_point` может вызываться, если " \
            "`self.grabbing_point` не `None`. В программе ошибка."
        self.x += x - self.grabbing_point[0]
        self.y += y - self.grabbing_point[1]
        self.grabbing_point = (x, y)
        msg = {
            'type': 'command',
            'command': {
                'type': 'coords',
                'id': self.id,
                'x1': self.x,
                'x2': self.x + self.size,
                'y1': self.y,
                'y2': self.y + self.size
            }
        }
        root.send_to_all_players(msg)

    def process_button_release_1(self, addr, event):
        if self.is_coord_missing(addr, event):
            return
        self.move_by_grabbing_point(addr, event['x'], event['y'])
        self.grabbing_point = None

    def process_b1_motion(self, addr, event):
        if self.is_coord_missing(addr, event):
            return
        self.move_by_grabbing_point(addr, event['x'], event['y'])

    def process_button_1(self, addr, event):
        if not self.are_x_and_y_ok(addr, event):
            return
        assert self.grabbing_point is None, \
            "Кубик по-прежнему кто-то удерживает. В программе ошибка, так " \
            "как проверка того, что кубик свободен должна выполняться в " \
            "методе `CubeCanvasServer.is_id_and_event_type_ok()`. Возможные " \
            "причины ошибки: неправильно обрабатываются " \
            "`self.grabbing_point` " \
            "или `CubeCanvasServer.grabbed_cubes_ids`"
        self.grabbing_point = (event['x'], event['y'])


class CubeCanvasServer:
    def __init__(self, master, num_cubes):
        self.master = master
        self.supported_incoming_event_types = \
            ['<Button-1>', '<ButtonRelease-1>', '<B1-Motion>']
        # Кубики располагаются внутри экземпляра `CubeCanvas` случайным
        # образом, но не ближе, чем `self.margin` к границе экземпляра
        # `CubeCanvas`.
        self.margin = 100
        self.cube_init_xrange = [
            self.margin,
            WINDOW_SHAPE[0] - self.margin
        ]
        self.cube_init_yrange = [
            self.margin,
            WINDOW_SHAPE[1] - self.margin
        ]
        self.cube_size_range = [15, 75]

        self.num_cubes = num_cubes
        # Ключи в словаре -- id объектов.
        self.cubes = {}
        self.create_cubes()

        self.grabbed_cubes_ids = {}

    def get_root(self):
        root = self.master
        while hasattr(root, 'master'):
            root = root.master
        return root

    def get_mode(self):
        return self.get_root().mode

    def get_free_id(self):
        id_ = 1
        taken_ids = list(self.cubes.keys())
        while id_ in taken_ids:
            id_ += 1
        return id_

    def create_cubes(self):
        for _ in range(self.num_cubes):
            x = rnd(*self.cube_init_xrange)
            y = rnd(*self.cube_init_yrange)
            size = rnd(*self.cube_size_range)
            color = choice(colors.INTENSIVE_RAINBOW)
            id_ = self.get_free_id()
            self.cubes[id_] = CubeServer(self, id_, x, y, size, color)

    def is_id_address_eventtype_ok(self, addr, event):
        ok = True
        conn = self.get_root().conns_to_clients[addr]
        oblig_part = {
            'type': 'error_msg',
            'error_class': 'ValueError',
            'addr': addr,
            'event': event
        }
        if event['type'] == '<Button-1>':
            if 'id' not in event:
                ok = False
                warning_msg = "Без ключа 'id' могут быть только словари " \
                    "с описаниями событий типа <ButtonRelease-1> и " \
                    "<B1-Motion>. В то время как у данного события тип " \
                    "{}".format(event['type'])
                warnings.warn(warning_msg)
                msg = dict(**oblig_part, msg=warning_msg)
                send_data_quite(conn, addr, msg)
            if 'id' in event and addr in self.grabbed_cubes_ids:
                ok = False
                warning_msg = "Игрок {} не может схватить кубик с id " \
                    "{}, пока не отпустит кубик с id {}. Вероятно, или в " \
                    "или в серверной части программы ошибка. Такие ситуации " \
                    "не должны возникать".format(
                        addr, event['id'], self.grabbed_cubes_ids[addr]
                    )
                warnings.warn(warning_msg)
                msg = dict(
                    **oblig_part,
                    msg=warning_msg,
                    grabbed_id=self.grabbed_cubes_ids[addr]
                )
                send_data_quite(conn, addr, msg)
            if 'id' in event and event['id'] not in self.cubes:
                ok = False
                present_ids = list(self.cubes.keys())
                warning_msg = "В canvas нет элемента с id {}. Или в " \
                    "клиентской, или в серверной части программы ошибка. \n" \
                    "id в наличии: {}\n".format(event['id'], present_ids)
                warnings.warn(warning_msg)
                msg = dict(**oblig_part, msg=warning_msg)
                send_data_quite(conn, addr, msg)
        elif event['type'] in ['<ButtonRelease-1>', '<B1-Motion>']:
            if 'id' in event:
                ok = False
                warning_msg = "Ключ id может быть только в словарях с " \
                    "описаниями событий типа <Button-1>. В то время как" \
                    "у данного события тип {}. Вероятно, в клиентской " \
                    "части программы неправильно реализовано составление " \
                    "сообщений данного типа".format(event['type'])
                warnings.warn(warning_msg)
                msg = dict(**oblig_part, msg=warning_msg)
                send_data_quite(conn, addr, msg)
            if addr not in self.grabbed_cubes_ids:
                ok = False
                warning_msg = "Адрес клиента, отправившего описание события " \
                    "типа <ButtonRelease-1> или <B1-Motion>, должен быть " \
                    "ключом словаря `self.grabbed_cubes_ids`."
                warnings.warn(warning_msg)
                msg = dict(**oblig_part, msg=warning_msg)
                send_data_quite(conn, addr, msg)
        else:
            ok = False
            warning_msg = "Только события типов {} поддерживаются, в то " \
                "время как было принято описание события типа {}. Вероятно, " \
                "в клиентской части программы допущена ошибка.".format(
                    self.supported_incoming_event_types, event['type'])
            warnings.warn(warning_msg)
            msg = dict(
                **oblig_part,
                msg=warning_msg,
                supported_event_types=self.supported_incoming_event_types
            )
            send_data_quite(conn, addr, msg)
        return ok

    def process_event(self, addr, event):
        if not self.is_id_address_eventtype_ok(addr, event):
            return
        if event['type'] == '<Button-1>':
            if self.cubes[event['id']].grabbing_point is None:
                assert event['id'] not in \
                    list(self.grabbed_cubes_ids.values()), \
                    "Если кубик свободен, id этого кубика не должно быть " \
                    "среди значений `self.grabbed_cubes_ids`. В серверной " \
                    "части программы ошибка."
                self.cubes[event['id']].process_button_1(addr, event)
                self.grabbed_cubes_ids[addr] = event['id']
        elif event['type'] in ['<ButtonRelease-1>', '<B1-Motion>']:
            event = copy.deepcopy(event)
            event['id'] = self.grabbed_cubes_ids[addr]
            if event['type'] == '<ButtonRelease-1>':
                self.cubes[event['id']].process_button_release_1(addr, event)
                del self.grabbed_cubes_ids[addr]
            elif event['type'] == '<B1-Motion>':
                self.cubes[event['id']].process_b1_motion(addr, event)
            else:
                assert False
        else:
            assert False

    def release_player_cube(self, addr):
        if addr in self.grabbed_cubes_ids:
            cube = self.cubes[self.grabbed_cubes_ids[addr]]
            cube.grabbing_point = None
            del self.grabbed_cubes_ids[addr]


class MainFrameServer:
    def __init__(self, master, num_cubes):
        self.master = master
        self.cube_canvas = CubeCanvasServer(self, num_cubes)

    def process_event(self, addr, event):
        self.cube_canvas.process_event(addr, event)


class CubeGameServer:
    def __init__(self, config):
        self.check_config(config)

        self.server_port = config['server_port']

        self.msg_types = ['error_msg', 'event']

        self.main_frame = MainFrameServer(self, config['num_cubes'])

        # `self.listener` -- сокет для установления соединения с клиентами.
        self.listener = socket.socket()
        self.listener.settimeout(0)
        self.listener.bind(('', self.server_port))
        self.listener.listen(MAX_NUM_PLAYERS)
        print(get_ip_address(), self.server_port)

        # Словарь сокетов для обмена данными с клиентами.
        # Ключи в словаре -- адреса игроков, значения -- сокеты.
        # Адрес -- кортеж из 2-х элементов ip и номера порта.
        self.conns_to_clients = {}

        self.players_scenarios = {}

    def mainloop(self):
        while True:
            self.connect_to_clients()
            self.guide_players()
            self.receive_from_clients()
            if DT_SECONDS > 0:
                time.sleep(DT_SECONDS)

    @staticmethod
    def check_config(config):
        if config['server_port'] > MAX_PORT_NUMBER \
                or config['server_port'] < MIN_PORT_NUMBER:
            raise ValueError(
                "Запрещенный номер порта:\n"
                "config['server_port'] = {}\n"
                "Разрещенные порты: {} - {}.".format(
                    config['server_port'], MIN_PORT_NUMBER, MAX_PORT_NUMBER)
            )
        if not (0 <= config['num_cubes'] <= MAX_NUM_CUBES):
            raise ValueError(
                "Количество кубиков в игре должно быть "
                "в диапазоне от {} до {}, в то время как\n"
                "config['num_cubes'] = {}".format(
                    0, MAX_NUM_CUBES, config['num_cubes'])
            )

    def connect_to_clients(self):
        if len(self.conns_to_clients) < MAX_NUM_PLAYERS:
            try:
                conn, addr = self.listener.accept()
                conn.settimeout(0)
                self.conns_to_clients[addr] = conn
                self.players_scenarios[addr] = PlayerScenario(self, addr)
            except BlockingIOError:
                pass

    def receive_from_clients(self):
        # Возможен обрыв соединения соединения и удаление элемента словаря.
        # Менять ключи элемента словаря в процессе итерации по нему запрещено.
        for addr in list(self.conns_to_clients):
            self.receive_from_client(addr)

    def receive_from_client(self, addr):
        try:
            msgs, e = recv_data(self.conns_to_clients[addr], addr)
            for msg in msgs:
                if msg['type'] == 'error_msg':
                    warnings.warn(
                        'Пришло сообщение об ошибке от игрока {}\n'
                        'Сообщение:\n'.format(addr) +
                        msg['msg']
                    )
                elif msg['type'] == 'event':
                    self.players_scenarios[addr].process_event(
                        addr, msg['event'])
                else:
                    warning_msg = "Сообщение неизвестного типа {} пришло "\
                        "от игрока {}.".format(repr(msg['type']), repr(addr))
                    warnings.warn(warning_msg)
                    msg = {
                        'type': 'error_msg',
                        'error_class': 'ValueError',
                        'addr': addr,
                        'msg': warning_msg,
                        'event': msg['event'],
                    }
                    send_data_quite(self.conns_to_clients[addr], addr, msg)
            if e is not None:
                raise e
        except CorruptedMessageError as e:
            warnings.warn(e.message)
        except BlockingIOError:
            pass
        except ConnectionResetError as e:
            warnings.warn(e)
            warnings.warn(CONNECTION_RESET_ERROR_WARNING_TMPL.format(addr))
            # FIXME
            # Непонятно когда возникает ошибка и потому не ясно следует ли
            # закрывать и удалять socket.
            self.conns_to_clients[addr].close()
            del self.conns_to_clients[addr]
            del self.players_scenarios[addr]
            self.main_frame.cube_canvas.release_player_cube(addr)
        except ConnectionAbortedError as e:
            warnings.warn(e)
            warnings.warn(CONNECTION_ABORTED_ERROR_WARNING_TMPL.format(addr))
            # FIXME
            # Неустановлено, в каких случаях возникает эта ошибка.
            # Она наблюдалась при выключении клиента, тогда закрытие и
            # удаление сокета -- правильное рещение. Однако, я наблюдал эту же
            # ошибку временном отключении wifi. В последнем случаем удаление
            # сокета приводит к необходимости перезапуска клиентской части
            # приложения.
            self.conns_to_clients[addr].close()
            del self.conns_to_clients[addr]
            del self.players_scenarios[addr]
            self.main_frame.cube_canvas.release_player_cube(addr)
        except Exception as e:
            warnings.warn(e)
            warnings.warn(
                "Для исключения типа {} не был написан обработчик. Возможно, "
                "стоит это сделать.".format(type(e))
            )

    def guide_players(self):
        for addr in self.conns_to_clients:
            self.players_scenarios[addr].act()

    def process_event(self, addr, event):
        self.main_frame.process_event(addr, event)

    def init_player(self, addr):
        cubes = self.main_frame.cube_canvas.cubes
        for cube in cubes.values():
            msg = {
                'type': 'command',
                'command': {
                    "type": 'add_cube',
                    "id": cube.id,
                    "x": cube.x,
                    "y": cube.y,
                    "size": cube.size,
                    "color": cube.color
                }
            }
            send_data_quite(self.conns_to_clients[addr], addr, msg)
        msg = {
            'type': 'command',
            'command': {
                "type": 'bind_all'
            }
        }
        send_data_quite(self.conns_to_clients[addr], addr, msg)

    def warn_events_before_init(self, addr, event):
        warning_msg = "На сервер от игрока {} пришло сообщение до " \
            "инициализации этого игрока.\nevent = {}".format(repr(addr), event)
        warnings.warn(warning_msg)
        msg = {
            'type': 'error_msg',
            'error_class': 'ValueError',
            'addr': addr,
            'msg': warning_msg,
            'event': event,
        }
        send_data_quite(self.conns_to_clients[addr], addr, msg)

    def close_all_sockets(self):
        self.listener.close()
        for conn in self.conns_to_clients.values():
            conn.close()

    def send_to_all_players(self, msg):
        for addr, conn in self.conns_to_clients.items():
            send_data_quite(conn, addr, msg)


def main():
    try:
        args = get_app_args()
        app = CubeGameServer(vars(args))
        app.mainloop()
    except KeyboardInterrupt as e:
        app.close_all_sockets()
        raise e


if __name__ == '__main__':
    main()

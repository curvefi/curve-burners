from fee_keeper.draft.collect.main import collect
from exchange.main import exchange
from utils import EPOCH
from forward.main import forward


# loop
def main():
    current_epoch = EPOCH.get_current()
    match current_epoch:
        case EPOCH.COLLECT:
            collect()
        case EPOCH.EXCHANGE:
            exchange()
        case EPOCH.FORWARD:
            forward()


if __name__ == '__main__':
    main()
